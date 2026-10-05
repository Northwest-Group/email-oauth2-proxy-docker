import enum
import re

import plugins.BasePlugin

SMTP_MAIL_FROM_MATCHER = re.compile(b'MAIL FROM: ?<(.*?)>(.*)\r\n', re.IGNORECASE)
SMTP_RCPT_TO_MATCHER = re.compile(b'RCPT TO: ?<(.*?)>(.*)\r\n', re.IGNORECASE)

HEADER_END = b'\r\n\r\n'
# match a whole header including any folded continuation lines
FROM_HEADER_MATCHER = re.compile(br'^From:.*\r\n(?:[ \t].*\r\n)*', re.IGNORECASE | re.MULTILINE)
REPLY_TO_HEADER_MATCHER = re.compile(br'^Reply-To:', re.IGNORECASE | re.MULTILINE)
ORIGINAL_FROM_HEADER_MATCHER = re.compile(br'^X-Original-From:', re.IGNORECASE | re.MULTILINE)
SUBJECT_HEADER_MATCHER = re.compile(br'^Subject:[ \t]*', re.IGNORECASE | re.MULTILINE)
RECIPIENT_HEADER_MATCHER = re.compile(br'^(?:To|Cc):.*\r\n(?:[ \t].*\r\n)*', re.IGNORECASE | re.MULTILINE)
ADDRESS_MATCHER = re.compile(br'[^\s<>,;:"\'()]+@[^\s<>,;:"\'()]+')

# Outlook shows internal senders by their directory name rather than the From header's display name, so senders can opt
# in to a subject tag by ending the local part of their address with the label keyword: edihq-label@example.com tags the
# subject as '[edihq] '. Prefix placeholders: {label} (local part without the keyword), {user} (local part), {sender}
DEFAULT_LABEL_KEYWORD = '-label'
DEFAULT_SUBJECT_PREFIX = '[{label}] '

# recipient_redirects maps specific recipient addresses to replacements (matched case-insensitively), for systems that
# cannot set their recipients correctly - e.g. VLTrader always sends to its own From address:
# {'VLTrader-label@thenorthwest.com': 'edi@thenorthwest.com'}. All other recipients are never changed


class SMTPAddressRewriter(plugins.BasePlugin.BasePlugin):
    class STATE(enum.Enum):
        NONE = 1
        MAIL_FROM = 2
        RCPT_TO = 3
        DATA = 4

    def __init__(self, static_sender=None, reply_to=None, label_keyword=DEFAULT_LABEL_KEYWORD,
                 subject_prefix=DEFAULT_SUBJECT_PREFIX, recipient_redirects=None):
        super().__init__()
        self.static_sender = static_sender.encode('utf-8') if static_sender else None
        self.reply_to = reply_to.encode('utf-8') if reply_to else None
        self.label_keyword = label_keyword.lower() if label_keyword else None
        self.subject_prefix = subject_prefix or None  # '' (or None/False) disables subject tagging
        self.recipient_redirects = {source.lower().encode('utf-8'): target.encode('utf-8')
                                    for source, target in (recipient_redirects or {}).items()}
        self.reset()

    def reset(self):
        self.sending_state = self.STATE.NONE
        self.previous_line_ended = False
        self.original_sender = None
        self.recipients = []
        self.header_processed = False
        self.header_buffer = b''

    def receive_from_client(self, byte_data):
        if self.sending_state == self.STATE.NONE:
            if SMTP_MAIL_FROM_MATCHER.match(byte_data):
                self.sending_state = self.STATE.MAIL_FROM
                return self.replace_mail_from(byte_data)
            if byte_data[:10].upper() == b'MAIL FROM:':
                self.log_info('Unrecognised MAIL FROM command - message will not be rewritten:', byte_data)
            return byte_data

        if len(byte_data) == 6 and byte_data.lower() == b'rset\r\n':
            self.reset()

        elif self.sending_state in (self.STATE.MAIL_FROM, self.STATE.RCPT_TO):
            if SMTP_RCPT_TO_MATCHER.match(byte_data):
                self.sending_state = self.STATE.RCPT_TO
                byte_data = self.replace_rcpt_to(byte_data)
            elif self.sending_state == self.STATE.RCPT_TO and byte_data.lower() == b'data\r\n':
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
            if self.static_sender:
                byte_data = b'MAIL FROM:<%b>%b\r\n' % (self.static_sender, match.group(2))  # keep SIZE= etc.
        return byte_data

    def replace_rcpt_to(self, byte_data):
        match = SMTP_RCPT_TO_MATCHER.match(byte_data)
        recipient = match.group(1)
        redirect = self.recipient_redirects.get(recipient.lower())
        if redirect:
            self.recipients.append('%s (redirected from %s)' % (
                redirect.decode('utf-8', 'replace'), recipient.decode('utf-8', 'replace')))
            return b'RCPT TO:<%b>%b\r\n' % (redirect, match.group(2))
        self.recipients.append(recipient.decode('utf-8', 'replace'))
        return byte_data

    def replace_recipient_headers(self, headers):
        if not self.recipient_redirects:
            return headers

        def replace_addresses(header):
            return ADDRESS_MATCHER.sub(
                lambda a: self.recipient_redirects.get(a.group(0).lower(), a.group(0)), header.group(0))

        return RECIPIENT_HEADER_MATCHER.sub(replace_addresses, headers)

    def replace_from_header(self, headers):
        if not self.static_sender or not self.original_sender:
            return headers

        new_from = b'From: "' + self.original_sender + b'" <' + self.static_sender + b'>\r\n'
        if self.reply_to and not REPLY_TO_HEADER_MATCHER.search(headers):  # keep a Reply-To set by the sending system
            new_from += b'Reply-To: <' + self.reply_to + b'>\r\n'
        if not ORIGINAL_FROM_HEADER_MATCHER.search(headers):
            new_from += b'X-Original-From: <' + self.original_sender + b'>\r\n'

        reply_to = 'kept existing' if REPLY_TO_HEADER_MATCHER.search(headers) else (
            '<%s>' % self.reply_to.decode('utf-8', 'replace') if self.reply_to else 'none')
        if FROM_HEADER_MATCHER.search(headers):
            headers = FROM_HEADER_MATCHER.sub(lambda _: new_from, headers, count=1)
        else:
            headers = new_from + headers

        headers = self.replace_recipient_headers(headers)
        tagged_headers = self.add_subject_prefix(headers)
        subject = SUBJECT_HEADER_MATCHER.search(tagged_headers)
        subject = tagged_headers[subject.end():].split(b'\r\n', 1)[0].decode('utf-8', 'replace') if subject else ''
        self.log_info('Rewrote sender <%s> as <%s>; to: %s; Reply-To: %s; subject %s: %s' % (
            self.original_sender.decode('utf-8', 'replace'), self.static_sender.decode('utf-8', 'replace'),
            ', '.join(self.recipients) or 'none', reply_to, 'tagged' if tagged_headers != headers else 'unchanged',
            subject))
        return tagged_headers

    def add_subject_prefix(self, headers):
        sender = self.original_sender.decode('utf-8', 'replace')
        user = sender.rsplit('@', 1)[0]
        if not self.subject_prefix or not self.label_keyword or not user.lower().endswith(self.label_keyword):
            return headers  # only senders using a label address get a subject tag

        label = user[:-len(self.label_keyword)]
        prefix = self.subject_prefix.format(label=label, user=user, sender=sender).encode('utf-8')
        match = SUBJECT_HEADER_MATCHER.search(headers)
        if not match:
            return headers + b'Subject: ' + prefix.rstrip() + b'\r\n'
        if headers[match.end():].startswith(prefix):  # already tagged (e.g. a resend)
            return headers
        return headers[:match.end()] + prefix + headers[match.end():]

    def receive_from_server(self, byte_data):
        return byte_data
