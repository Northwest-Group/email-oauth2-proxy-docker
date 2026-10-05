import enum
import fnmatch
import re

import plugins.BasePlugin

SMTP_MAIL_FROM_MATCHER = re.compile(b'MAIL FROM:<(.*?)>(.*)\r\n', re.IGNORECASE)
SMTP_RCPT_TO_MATCHER = re.compile(b'RCPT TO:.+\r\n', re.IGNORECASE)

HEADER_END = b'\r\n\r\n'
# match a whole header including any folded continuation lines
FROM_HEADER_MATCHER = re.compile(br'^From:.*\r\n(?:[ \t].*\r\n)*', re.IGNORECASE | re.MULTILINE)
REPLY_TO_HEADER_MATCHER = re.compile(br'^Reply-To:', re.IGNORECASE | re.MULTILINE)
ORIGINAL_FROM_HEADER_MATCHER = re.compile(br'^X-Original-From:', re.IGNORECASE | re.MULTILINE)
SUBJECT_HEADER_MATCHER = re.compile(br'^Subject:[ \t]*', re.IGNORECASE | re.MULTILINE)

# Outlook shows internal senders by their directory name rather than the From header's display name, so by default the
# subject is also tagged with the sending system's address. Placeholders: {sender} (full address) and {user} (local part)
DEFAULT_SUBJECT_PREFIX = '[{user}] '
OVERRIDE_OPTIONS = {'rewrite', 'static_sender', 'reply_to', 'subject_prefix'}


class SMTPAddressRewriter(plugins.BasePlugin.BasePlugin):
    class STATE(enum.Enum):
        NONE = 1
        MAIL_FROM = 2
        RCPT_TO = 3
        DATA = 4

    def __init__(self, static_sender=None, reply_to=None, subject_prefix=DEFAULT_SUBJECT_PREFIX, overrides=None):
        super().__init__()
        self.defaults = {'rewrite': True, 'static_sender': static_sender, 'reply_to': reply_to,
                         'subject_prefix': subject_prefix}

        # per-sender settings, keyed by address or wildcard pattern (e.g. '*@thenorthwest.com'); an exact address
        # takes priority over patterns, then patterns are checked in the order given. Any option above can be
        # overridden, and {'rewrite': False} passes the sender's messages through completely unchanged
        self.overrides = []
        for pattern, options in (overrides or {}).items():
            unknown = set(options) - OVERRIDE_OPTIONS
            if unknown:
                raise ValueError('Unknown SMTPAddressRewriter override option(s) for %s: %s' % (pattern, unknown))
            self.overrides.append((pattern.lower(), options))
        self.overrides.sort(key=lambda override: any(c in override[0] for c in '*?['))  # stable: exact first

        self.reset()

    def reset(self):
        self.sending_state = self.STATE.NONE
        self.previous_line_ended = False
        self.original_sender = None
        self.header_processed = False
        self.header_buffer = b''
        self.apply_settings(self.defaults)

    def apply_settings(self, settings):
        self.rewrite = settings.get('rewrite', True)
        self.static_sender = settings['static_sender'].encode('utf-8') if settings.get('static_sender') else None
        self.reply_to = settings['reply_to'].encode('utf-8') if settings.get('reply_to') else None
        self.subject_prefix = settings.get('subject_prefix') or None  # '' (or None/False) disables the prefix

    def settings_for(self, sender):
        sender = sender.decode('utf-8', 'replace').lower()
        for pattern, options in self.overrides:
            if fnmatch.fnmatchcase(sender, pattern):
                self.log_debug('Applying override', pattern, 'for sender', sender)
                return {**self.defaults, **options}
        return self.defaults

    def receive_from_client(self, byte_data):
        if self.sending_state == self.STATE.NONE:
            if SMTP_MAIL_FROM_MATCHER.match(byte_data):
                self.sending_state = self.STATE.MAIL_FROM
                return self.replace_mail_from(byte_data)
            return byte_data

        if len(byte_data) == 6 and byte_data.lower() == b'rset\r\n':
            self.reset()

        elif self.sending_state == self.STATE.MAIL_FROM:
            if SMTP_RCPT_TO_MATCHER.match(byte_data):
                self.sending_state = self.STATE.RCPT_TO

        elif self.sending_state == self.STATE.RCPT_TO:
            if byte_data.lower() == b'data\r\n':
                self.sending_state = self.STATE.DATA

        elif self.sending_state == self.STATE.DATA:
            end_of_message = byte_data.endswith(b'\r\n.\r\n') or (self.previous_line_ended and byte_data == b'.\r\n')
            self.previous_line_ended = byte_data.endswith(b'\r\n')

            if not self.header_processed:
                # only ever edit the top-level header block; hold data back until all of it has arrived
                self.header_buffer += byte_data
                header_end = self.header_buffer.find(HEADER_END)
                if header_end == -1 and not end_of_message:
                    return None  # consumed for now; released once the header block is complete
                split = header_end + 2 if header_end != -1 else len(self.header_buffer)
                byte_data = self.replace_from_header(self.header_buffer[:split]) + self.header_buffer[split:]
                self.header_buffer = b''
                self.header_processed = True

            if end_of_message:
                self.reset()

        return byte_data

    def replace_mail_from(self, byte_data):
        match = SMTP_MAIL_FROM_MATCHER.match(byte_data)
        if match:
            self.original_sender = match.group(1)
            self.apply_settings(self.settings_for(self.original_sender))
            if self.rewrite and self.static_sender:
                byte_data = b'MAIL FROM:<%b>%b\r\n' % (self.static_sender, match.group(2))  # keep SIZE= etc.
        return byte_data

    def replace_from_header(self, headers):
        if not self.rewrite or not self.static_sender or not self.original_sender:
            return headers

        new_from = b'From: "' + self.original_sender + b'" <' + self.static_sender + b'>\r\n'
        if self.reply_to and not REPLY_TO_HEADER_MATCHER.search(headers):  # keep a Reply-To set by the sending system
            new_from += b'Reply-To: <' + self.reply_to + b'>\r\n'
        if not ORIGINAL_FROM_HEADER_MATCHER.search(headers):
            new_from += b'X-Original-From: <' + self.original_sender + b'>\r\n'

        if FROM_HEADER_MATCHER.search(headers):
            headers = FROM_HEADER_MATCHER.sub(lambda _: new_from, headers, count=1)
        else:
            headers = new_from + headers
        return self.add_subject_prefix(headers)

    def add_subject_prefix(self, headers):
        if not self.subject_prefix:
            return headers

        sender = self.original_sender.decode('utf-8', 'replace')
        prefix = self.subject_prefix.format(sender=sender, user=sender.split('@')[0]).encode('utf-8')
        match = SUBJECT_HEADER_MATCHER.search(headers)
        if not match:
            return headers + b'Subject: ' + prefix.rstrip() + b'\r\n'
        if headers[match.end():].startswith(prefix):  # already tagged (e.g. a resend)
            return headers
        return headers[:match.end()] + prefix + headers[match.end():]

    def receive_from_server(self, byte_data):
        return byte_data
