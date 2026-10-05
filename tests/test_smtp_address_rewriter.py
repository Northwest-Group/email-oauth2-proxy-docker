"""Tests for plugins/SMTPAddressRewriter.py. The proxy passes raw socket reads (up to 64KB, split at arbitrary
points) to plugins, so messages are fed in chunks to make sure only the top-level headers are ever edited."""

import email
import email.policy
import os
import random
import sys
import unittest
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from plugins.SMTPAddressRewriter import SMTPAddressRewriter  # noqa: E402

STATIC_SENDER = 'noreply-integrations@thenorthwest.com'
SYSTEM_SENDER = 'system.A.Box.2@cyx.com'
PDF = b'%PDF-1.4\n' + random.Random(0).randbytes(300_000) + b'\n%%EOF\n'


def build_message(reply_to=None):
    message = EmailMessage()
    message['From'] = 'System A <%s>' % SYSTEM_SENDER
    message['To'] = 'someone@example.com'
    message['Subject'] = 'Report'
    if reply_to:
        message['Reply-To'] = reply_to
    message.set_content('See attached.\n\nFrom: a body line that must not be touched\n')
    message.add_attachment(PDF, maintype='application', subtype='pdf', filename='report.pdf')
    raw = message.as_bytes(policy=email.policy.SMTP).replace(b'\r\n.', b'\r\n..')  # SMTP dot-stuffing
    return raw + b'.\r\n'


def send(plugin, message, chunk_sizes, mail_from=b'MAIL FROM:<%s>\r\n' % SYSTEM_SENDER.encode()):
    output = b''

    def receive(byte_data):
        nonlocal output
        output += plugin.receive_from_client(byte_data) or b''

    for command in (mail_from, b'RCPT TO:<someone@example.com>\r\n', b'DATA\r\n'):
        receive(command)
    position = 0
    while position < len(message):
        size = next(chunk_sizes)
        receive(message[position:position + size])
        position += size
    return output


def parse(output):
    start = output.index(b'DATA\r\n') + 6
    end = output.index(b'\r\n.\r\n', start) + 2
    return email.message_from_bytes(output[start:end].replace(b'\r\n..', b'\r\n.'), policy=email.policy.default)


def max_chunks():
    while True:
        yield 65536


def random_chunks(seed=1):
    rng = random.Random(seed)
    while True:
        yield rng.randint(1, 20000)


class TestSMTPAddressRewriter(unittest.TestCase):
    def test_attachment_intact(self):
        for name, chunks in (('64KB reads', max_chunks()), ('random reads', random_chunks())):
            with self.subTest(name):
                plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
                message = parse(send(plugin, build_message(), chunks))
                attachment = next(message.iter_attachments())
                self.assertEqual(attachment.get_content(), PDF)

    def test_headers_rewritten(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
        output = send(plugin, build_message(), random_chunks())
        self.assertTrue(output.startswith(b'MAIL FROM:<%s>\r\n' % STATIC_SENDER.encode()))
        message = parse(output)
        self.assertEqual(message['From'].addresses[0].addr_spec, STATIC_SENDER)
        self.assertEqual(message['From'].addresses[0].display_name, SYSTEM_SENDER)
        self.assertEqual(message.get_all('Reply-To'), ['edi@thenorthwest.com'])
        self.assertIn(b'From: a body line that must not be touched', output)
        self.assertEqual(output.count(b'From: "'), 1)

    def test_existing_reply_to_kept(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
        message = parse(send(plugin, build_message(reply_to='tickets@thenorthwest.com'), random_chunks()))
        self.assertEqual(message.get_all('Reply-To'), ['tickets@thenorthwest.com'])

    def test_no_reply_to_configured(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        message = parse(send(plugin, build_message(), random_chunks()))
        self.assertIsNone(message['Reply-To'])

    def test_mail_from_parameters_kept(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        output = send(plugin, build_message(), random_chunks(),
                      mail_from=b'MAIL FROM:<%s> SIZE=12345\r\n' % SYSTEM_SENDER.encode())
        self.assertTrue(output.startswith(b'MAIL FROM:<%s> SIZE=12345\r\n' % STATIC_SENDER.encode()))

    def test_multiple_messages_per_session(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        chunks = random_chunks()
        send(plugin, build_message(), chunks)
        output = send(plugin, build_message(), chunks)
        self.assertTrue(output.startswith(b'MAIL FROM:<%s>\r\n' % STATIC_SENDER.encode()))
        self.assertEqual(next(parse(output).iter_attachments()).get_content(), PDF)


if __name__ == '__main__':
    unittest.main()
