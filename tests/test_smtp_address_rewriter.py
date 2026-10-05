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
LABEL_SENDER = 'edihq-label@thenorthwest.com'
PDF = b'%PDF-1.4\n' + random.Random(0).randbytes(300_000) + b'\n%%EOF\n'


def build_message(reply_to=None, subject='Report', to='someone@example.com', cc=None):
    message = EmailMessage()
    message['From'] = 'System A <%s>' % SYSTEM_SENDER
    message['To'] = to
    if cc:
        message['Cc'] = cc
    if subject is not None:
        message['Subject'] = subject
    if reply_to:
        message['Reply-To'] = reply_to
    message.set_content('See attached.\n\nFrom: a body line that must not be touched\n')
    message.add_attachment(PDF, maintype='application', subtype='pdf', filename='report.pdf')
    raw = message.as_bytes(policy=email.policy.SMTP).replace(b'\r\n.', b'\r\n..')  # SMTP dot-stuffing
    return raw + b'.\r\n'


def send(plugin, message, chunk_sizes, mail_from=b'MAIL FROM:<%s>\r\n' % SYSTEM_SENDER.encode(),
         rcpt_to=(b'RCPT TO:<someone@example.com>\r\n',)):
    output = b''

    def receive(byte_data):
        nonlocal output
        output += plugin.receive_from_client(byte_data) or b''

    for command in (mail_from, *rcpt_to, b'DATA\r\n'):
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
        self.assertEqual(message['X-Original-From'], '<%s>' % SYSTEM_SENDER)
        self.assertEqual(message['Subject'], 'Report')  # no label keyword: rewritten, but subject untouched
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

    def label_message(self, plugin, subject='Report', sender=LABEL_SENDER):
        mail_from = b'MAIL FROM:<%s>\r\n' % sender.encode()
        output = send(plugin, build_message(subject=subject), random_chunks(), mail_from=mail_from)
        self.assertTrue(output.startswith(b'MAIL FROM:<%s>\r\n' % STATIC_SENDER.encode()))  # always rewritten
        return parse(output)

    def test_label_address_tags_subject(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
        message = self.label_message(plugin)
        self.assertEqual(message['Subject'], '[edihq] Report')
        self.assertEqual(message['From'].addresses[0].display_name, LABEL_SENDER)
        self.assertEqual(message['X-Original-From'], '<%s>' % LABEL_SENDER)
        self.assertEqual(message.get_all('Reply-To'), ['edi@thenorthwest.com'])

    def test_label_keyword_case_insensitive(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        message = self.label_message(plugin, sender='EDIHQ-Label@thenorthwest.com')
        self.assertEqual(message['Subject'], '[EDIHQ] Report')

    def test_keyword_only_at_end_of_local_part(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        for sender in ('edihq@label.com', 'edi-labelhq@thenorthwest.com', 'edihq@thenorthwest-label.com'):
            with self.subTest(sender):
                self.assertEqual(self.label_message(plugin, sender=sender)['Subject'], 'Report')

    def test_custom_keyword_and_format(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, label_keyword='+tag', subject_prefix='({label}) ')
        self.assertEqual(self.label_message(plugin, sender='hq+tag@thenorthwest.com')['Subject'], '(hq) Report')
        self.assertEqual(self.label_message(plugin)['Subject'], 'Report')

    def test_subject_prefix_disabled(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, subject_prefix='')
        message = self.label_message(plugin)
        self.assertEqual(message['Subject'], 'Report')
        self.assertEqual(message['From'].addresses[0].addr_spec, STATIC_SENDER)

    def test_subject_prefix_not_duplicated(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        self.assertEqual(self.label_message(plugin, subject='[edihq] Report')['Subject'], '[edihq] Report')

    def test_subject_added_when_missing(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        self.assertEqual(self.label_message(plugin, subject=None)['Subject'], '[edihq]')

    def test_encoded_and_folded_subject(self):
        subject = 'Rapport de livraison \u2013 ' + 'tr\u00e8s long sujet ' * 6
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        self.assertEqual(self.label_message(plugin, subject=subject)['Subject'], '[edihq] ' + subject)

    def test_logs_each_rewrite(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
        logged = []
        plugin.log_info = lambda *args: logged.append(' '.join(str(a) for a in args))
        self.label_message(plugin)
        self.assertEqual(logged, ['Rewrote sender <%s> as <%s>; to: someone@example.com; Reply-To: '
                                  '<edi@thenorthwest.com>; subject tagged: [edihq] Report' % (LABEL_SENDER, STATIC_SENDER)])

    def test_mail_from_with_space(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        output = send(plugin, build_message(), random_chunks(),
                      mail_from=b'MAIL FROM: <%s>\r\n' % LABEL_SENDER.encode())
        self.assertTrue(output.startswith(b'MAIL FROM:<%s>\r\n' % STATIC_SENDER.encode()))
        self.assertEqual(parse(output)['Subject'], '[edihq] Report')

    def test_unrecognised_mail_from_logged(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER)
        logged = []
        plugin.log_info = lambda *args: logged.append(args)
        self.assertEqual(plugin.receive_from_client(b'MAIL FROM:someone@example.com\r\n'),
                         b'MAIL FROM:someone@example.com\r\n')
        self.assertEqual(len(logged), 1)

    def test_recipient_redirected(self):
        # VLTrader sends to its own (label) From address; that mailbox does not exist, so it is redirected by config
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com',
                                     recipient_redirects={'VLTrader-label@thenorthwest.com': 'edi@thenorthwest.com'})
        logged = []
        plugin.log_info = lambda *args: logged.append(' '.join(str(a) for a in args))
        sender = b'vltrader-LABEL@thenorthwest.com'  # matching ignores case
        output = send(plugin, build_message(to='<%s>' % sender.decode(), cc='Bob <bob@example.com>'), random_chunks(),
                      mail_from=b'MAIL FROM:<%s>\r\n' % sender,
                      rcpt_to=(b'RCPT TO:<%s>\r\n' % sender, b'RCPT TO: <bob@example.com> NOTIFY=NEVER\r\n'))
        self.assertIn(b'\r\nRCPT TO:<edi@thenorthwest.com>\r\n', output)
        self.assertIn(b'\r\nRCPT TO: <bob@example.com> NOTIFY=NEVER\r\n', output)
        self.assertNotIn(b'RCPT TO:<%s>' % sender, output)
        message = parse(output)
        self.assertEqual(message['To'].addresses[0].addr_spec, 'edi@thenorthwest.com')
        self.assertEqual(message['Cc'].addresses[0].addr_spec, 'bob@example.com')
        self.assertEqual(message['From'].addresses[0].display_name, sender.decode())  # From label unchanged
        self.assertEqual(message['Subject'], '[vltrader] Report')
        self.assertEqual(next(message.iter_attachments()).get_content(), PDF)
        self.assertIn('to: edi@thenorthwest.com (redirected from %s), bob@example.com;' % sender.decode(), logged[0])

    def test_other_recipients_untouched(self):
        # only configured addresses are redirected - other label addresses and real recipients are left alone
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com',
                                     recipient_redirects={'VLTrader-label@thenorthwest.com': 'edi@thenorthwest.com'})
        to = '%s, someone@example.com' % LABEL_SENDER
        output = send(plugin, build_message(to=to), random_chunks(),
                      rcpt_to=(b'RCPT TO:<%s>\r\n' % LABEL_SENDER.encode(), b'RCPT TO:<someone@example.com>\r\n'))
        self.assertIn(b'RCPT TO:<%s>\r\nRCPT TO:<someone@example.com>\r\n' % LABEL_SENDER.encode(), output)
        self.assertEqual(parse(output)['To'], to)

    def test_no_redirects_by_default(self):
        plugin = SMTPAddressRewriter(static_sender=STATIC_SENDER, reply_to='edi@thenorthwest.com')
        rcpt = b'RCPT TO:<VLTrader-label@thenorthwest.com>\r\n'
        output = send(plugin, build_message(to='VLTrader-label@thenorthwest.com'), random_chunks(), rcpt_to=(rcpt,))
        self.assertIn(rcpt, output)
        self.assertEqual(parse(output)['To'], 'VLTrader-label@thenorthwest.com')

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
