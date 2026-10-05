import re
import enum
import plugins.BasePlugin

SMTP_MAIL_FROM_MATCHER = re.compile(b'MAIL FROM:<(.*?)>(.*)\r\n', re.IGNORECASE)
SMTP_RCPT_TO_MATCHER = re.compile(b'RCPT TO:.+\r\n', re.IGNORECASE)

HEADER_END = b'\r\n\r\n'
# match a whole header including any folded continuation lines
FROM_HEADER_MATCHER = re.compile(br'^From:.*\r\n(?:[ \t].*\r\n)*', re.IGNORECASE | re.MULTILINE)
REPLY_TO_HEADER_MATCHER = re.compile(br'^Reply-To:', re.IGNORECASE | re.MULTILINE)


class SMTPAddressRewriter(plugins.BasePlugin.BasePlugin):
    class STATE(enum.Enum):
        NONE = 1
        MAIL_FROM = 2
        RCPT_TO = 3
        DATA = 4

    def __init__(self, static_sender=None, reply_to=None):
        super().__init__()
        self.static_sender = static_sender.encode('utf-8') if static_sender else None
        self.reply_to = reply_to.encode('utf-8') if reply_to else None
        self.reset()

    def reset(self):
        self.sending_state = self.STATE.NONE
        self.previous_line_ended = False
        self.original_sender = None
        self.header_processed = False
        self.header_buffer = b''

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
            if self.static_sender:
                byte_data = b'MAIL FROM:<%b>%b\r\n' % (self.static_sender, match.group(2))  # keep SIZE= etc.
        return byte_data

    def replace_from_header(self, headers):
        if not self.static_sender or not self.original_sender:
            return headers

        new_from = b'From: "' + self.original_sender + b'" <' + self.static_sender + b'>\r\n'
        if self.reply_to and not REPLY_TO_HEADER_MATCHER.search(headers):  # keep a Reply-To set by the sending system
            new_from += b'Reply-To: <' + self.reply_to + b'>\r\n'

        if FROM_HEADER_MATCHER.search(headers):
            return FROM_HEADER_MATCHER.sub(lambda _: new_from, headers, count=1)
        return new_from + headers

    def receive_from_server(self, byte_data):
        return byte_data
