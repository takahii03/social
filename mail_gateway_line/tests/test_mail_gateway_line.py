# Copyright 2026 Takahiro Sunaga
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from odoo.addons.mail_gateway.tests.common import MailGatewayTestCase
from odoo.addons.mail_gateway_line.models import mail_gateway_line

USER_ID = "U4af4980629"
GROUP_ID = "Ca56f94637c"
ROOM_ID = "Ra8dbf4673c"
WEBHOOK_KEY = "demo_hook"
CHANNEL_SECRET = "channel-secret"
SENT_MESSAGE_ID = "461230966842064897"
BASE_URL = "https://odoo.example.com"
SERVICE_LOGGER = "odoo.addons.mail_gateway_line.models.mail_gateway_line"
CONTROLLER_LOGGER = "odoo.addons.mail_gateway.controllers.gateway"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(self.payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


class FakeHttpRequest:
    """Just enough of a werkzeug request for _verify_update."""

    def __init__(self, data, signature=None):
        self.data = data
        self.headers = {}
        if signature is not None:
            self.headers["X-Line-Signature"] = signature


def fake_get(url, **kwargs):
    if "/content" in url:
        return FakeResponse(
            content=b"binary_data", headers={"Content-Type": "image/jpeg"}
        )
    if "/summary" in url:
        return FakeResponse(payload={"groupName": "Plant lovers"})
    if "/profile/" in url or "/member/" in url:
        return FakeResponse(payload={"displayName": "LINE Taro", "userId": USER_ID})
    return FakeResponse()


def fake_push(url, **kwargs):
    return FakeResponse(payload={"sentMessages": [{"id": SENT_MESSAGE_ID}]})


@tagged("-at_install", "post_install")
class LineCase(MailGatewayTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # LINE downloads the attachments itself, so the module refuses to send
        # one while the instance does not advertise a public HTTPS address.
        cls.env["ir.config_parameter"].sudo().set_param("web.base.url", BASE_URL)
        cls.gateway = cls.env["mail.gateway"].create(
            {
                "name": "LINE gateway",
                "gateway_type": "line",
                "token": "channel-access-token",
                "webhook_secret": CHANNEL_SECRET,
                "webhook_key": WEBHOOK_KEY,
                "member_ids": [(4, cls.env.user.id)],
            }
        )
        cls.service = cls.env["mail.gateway.line"]
        cls.text_event = cls._event(
            {
                "type": "message",
                "webhookEventId": "01FZ74A0TDDPYRVKNK77XKC3ZR",
                "source": {"type": "user", "userId": USER_ID},
                "replyToken": "0f3779fba3b349968c5d07db31eab56f",
                "message": {"id": "325708", "type": "text", "text": "Is it ok?"},
            }
        )
        cls.image_event = cls._event(
            {
                "type": "message",
                "webhookEventId": "01FZ74ASS536FW97EX38NKCZQK",
                "source": {"type": "user", "userId": USER_ID},
                "message": {
                    "id": "354718",
                    "type": "image",
                    "contentProvider": {"type": "line"},
                },
            }
        )
        cls.postback_event = cls._event(
            {
                "type": "postback",
                "webhookEventId": "01FZ74ASS536FW97EX38NKCZQL",
                "source": {"type": "user", "userId": USER_ID},
                "postback": {"data": "lot=42"},
            }
        )

    @classmethod
    def _event(cls, event):
        event.setdefault("timestamp", 1462629479859)
        return {"destination": "Ub9f7f7b8b", "events": [event]}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def integrate(self):
        with patch("requests.put") as put_mock:
            put_mock.return_value = FakeResponse()
            self.gateway.set_webhook()
        self.assertEqual(self.gateway.integrated_webhook_state, "integrated")

    def post_event(self, payload, signature=True, webhook_key=WEBHOOK_KEY):
        data = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        if signature:
            digest = hmac.new(
                CHANNEL_SECRET.encode(), data.encode(), hashlib.sha256
            ).digest()
            headers["X-Line-Signature"] = base64.b64encode(digest).decode()
        return self.url_open(
            f"/gateway/line/{webhook_key}/update", data=data, headers=headers
        )

    def channels(self):
        return self.env["discuss.channel"].search(
            [("gateway_id", "=", self.gateway.id)]
        )

    def receive(self, payload, get_mock=None, post_mock=None, **kwargs):
        """Post a webhook payload the way LINE does.

        Everything the event needs happens while the request is handled, so
        the calls to the API have to be mocked around it.
        """
        self.integrate()
        with (
            patch("requests.get", side_effect=get_mock or fake_get),
            patch("requests.post", side_effect=post_mock or fake_push),
        ):
            self.post_event(payload, **kwargs)
        return self.channels()


class TestWebhook(LineCase):
    def test_webhook_management(self):
        self.assertTrue(self.gateway.can_set_webhook)
        self.integrate()
        self.gateway.remove_webhook()
        self.assertFalse(self.gateway.integrated_webhook_state)

    def test_webhook_needs_a_channel_secret(self):
        """Without the channel secret no request can be verified."""
        self.gateway.webhook_secret = False
        self.assertFalse(self.gateway.can_set_webhook)

    def test_set_webhook_failure_is_reported(self):
        with patch("requests.put") as put_mock:
            put_mock.side_effect = requests.exceptions.ConnectionError("boom")
            with self.assertRaises(UserError):
                self.gateway.set_webhook()

    @mute_logger(CONTROLLER_LOGGER)
    def test_wrong_signature_is_rejected(self):
        self.integrate()
        data = json.dumps(self.text_event)
        self.url_open(
            f"/gateway/line/{WEBHOOK_KEY}/update",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": "not-the-right-signature",
            },
        )
        self.assertFalse(self.channels())

    @mute_logger(CONTROLLER_LOGGER)
    def test_missing_signature_is_rejected(self):
        self.integrate()
        self.post_event(self.text_event, signature=False)
        self.assertFalse(self.channels())

    def test_signature_of_a_non_ascii_header_is_rejected(self):
        """A header that is not ascii must be refused, not raise."""
        body = json.dumps(self.text_event).encode()
        fake = FakeHttpRequest(body, signature="\xe9")
        with patch.object(
            mail_gateway_line, "request", SimpleNamespace(httprequest=fake)
        ):
            self.assertFalse(
                self.service._verify_update(
                    {"webhook_secret": CHANNEL_SECRET}, self.text_event
                )
            )

    def test_signature_of_the_right_body_is_accepted(self):
        body = json.dumps(self.text_event).encode()
        digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
        fake = FakeHttpRequest(body, signature=base64.b64encode(digest).decode())
        with patch.object(
            mail_gateway_line, "request", SimpleNamespace(httprequest=fake)
        ):
            self.assertTrue(
                self.service._verify_update(
                    {"webhook_secret": CHANNEL_SECRET}, self.text_event
                )
            )

    @mute_logger(SERVICE_LOGGER)
    def test_payload_that_is_not_an_object_is_ignored(self):
        self.service._receive_update(self.gateway, ["not", "an", "object"])
        self.assertFalse(self.channels())


class TestReceive(LineCase):
    def test_receive_text(self):
        channel = self.receive(self.text_event)
        self.assertTrue(channel)
        self.assertEqual(channel.gateway_channel_token, USER_ID)
        self.assertEqual(channel.name, "LINE Taro")
        message = channel.message_ids
        self.assertEqual(len(message), 1)
        self.assertIn("Is it ok?", message.body)
        self.assertEqual(message.gateway_type, "line")
        # Nobody is linked yet, so the author is a guest
        self.assertFalse(message.author_id)
        self.assertTrue(
            self.env["mail.guest"].search(
                [("gateway_id", "=", self.gateway.id), ("gateway_token", "=", USER_ID)]
            )
        )

    def test_receive_text_from_known_partner(self):
        partner = self.env["res.partner"].create({"name": "Taro"})
        self.env["res.partner.gateway.channel"].create(
            {
                "partner_id": partner.id,
                "gateway_id": self.gateway.id,
                "gateway_token": USER_ID,
            }
        )
        channel = self.receive(self.text_event)
        self.assertEqual(channel.message_ids.author_id, partner)
        # The conversation is named after the partner we already know
        self.assertEqual(channel.name, "Taro")

    def test_the_profile_is_read_once_per_new_channel(self):
        self.integrate()
        with (
            patch("requests.get", side_effect=fake_get) as get_mock,
            patch("requests.post", side_effect=fake_push),
        ):
            self.post_event(self.text_event)
        profile_calls = [
            call for call in get_mock.call_args_list if "/profile/" in call.args[0]
        ]
        self.assertEqual(len(profile_calls), 1)

    def test_receive_image(self):
        channel = self.receive(self.image_event)
        attachment = channel.message_ids.attachment_ids
        self.assertEqual(len(attachment), 1)
        self.assertTrue(attachment.name.startswith("354718"))
        self.assertEqual(attachment.raw, b"binary_data")

    def test_receive_image_of_an_external_provider_keeps_the_message(self):
        """A picture Odoo cannot download must not make the inquiry vanish."""
        event = json.loads(json.dumps(self.image_event))
        event["events"][0]["message"]["contentProvider"] = {
            "type": "external",
            "originalContentUrl": "https://example.com/image.jpg",
        }
        channel = self.receive(event)
        self.assertTrue(channel.message_ids)
        self.assertFalse(channel.message_ids.attachment_ids)

    @mute_logger(SERVICE_LOGGER)
    def test_a_content_that_is_gone_keeps_the_message(self):
        """The content expires on the LINE side, the inquiry must not."""

        def gone(url, **kwargs):
            if "/content" in url:
                return FakeResponse(status_code=404)
            return fake_get(url, **kwargs)

        channel = self.receive(self.image_event, get_mock=gone)
        self.assertTrue(channel.message_ids)
        self.assertFalse(channel.message_ids.attachment_ids)
        # The wording is translated, the type of what was received is not
        self.assertIn("image", channel.message_ids.body)

    def test_a_content_that_fails_asks_line_to_deliver_the_event_again(self):
        """Nothing retries here, so the failure has to reach the webhook.

        Answering something else than a 2xx is what makes LINE deliver the
        event again; swallowing it would lose the inquiry for good.
        """

        def unavailable(url, **kwargs):
            if "/content" in url:
                return FakeResponse(status_code=503)
            return fake_get(url, **kwargs)

        with patch("requests.get", side_effect=unavailable):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.service._receive_update(self.gateway, self.image_event)
        self.assertFalse(self.channels().message_ids)

    def test_receive_location(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["message"] = {
            "id": "325709",
            "type": "location",
            "title": "The shop",
            "address": "Somewhere",
            "latitude": 35.65,
            "longitude": 139.7,
        }
        channel = self.receive(event)
        self.assertIn("35.65,139.7", channel.message_ids.body)
        self.assertIn("The shop", channel.message_ids.body)

    def test_receive_location_without_coordinates(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["message"] = {
            "id": "325710",
            "type": "location",
            "address": "Somewhere",
        }
        channel = self.receive(event)
        self.assertIn("Somewhere", channel.message_ids.body)

    def test_receive_sticker(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["message"] = {
            "id": "325711",
            "type": "sticker",
            "keywords": ["Happy", "Thank you"],
        }
        channel = self.receive(event)
        self.assertIn("Happy", channel.message_ids.body)

    def test_an_unsupported_message_type_keeps_the_inquiry(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["message"] = {"id": "325712", "type": "imagemap"}
        channel = self.receive(event)
        self.assertEqual(len(channel.message_ids), 1)
        self.assertIn("imagemap", channel.message_ids.body)

    def test_receive_postback(self):
        channel = self.receive(self.postback_event)
        message = channel.message_ids
        self.assertIn("lot=42", message.body)
        # Recorded as a notification, so it is never sent back to LINE
        self.assertEqual(message.message_type, "notification")

    def test_receive_postback_with_params(self):
        event = json.loads(json.dumps(self.postback_event))
        event["events"][0]["postback"]["params"] = {"date": "2026-09-08"}
        channel = self.receive(event)
        self.assertIn("date=2026-09-08", channel.message_ids.body)

    def test_receive_follow_opens_the_conversation(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0] = {
            "type": "follow",
            "webhookEventId": "01FZ74ASS536FW97EX38NKCZQM",
            "timestamp": 1462629479859,
            "source": {"type": "user", "userId": USER_ID},
        }
        channel = self.receive(event)
        self.assertEqual(channel.gateway_channel_token, USER_ID)
        self.assertFalse(channel.message_ids)

    def test_receive_from_a_group(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["source"] = {
            "type": "group",
            "groupId": GROUP_ID,
            "userId": USER_ID,
        }
        channel = self.receive(event)
        self.assertEqual(channel.gateway_channel_token, GROUP_ID)
        self.assertEqual(channel.name, "Plant lovers")

    def test_receive_from_a_room(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["source"] = {
            "type": "room",
            "roomId": ROOM_ID,
            "userId": USER_ID,
        }
        channel = self.receive(event)
        self.assertEqual(channel.gateway_channel_token, ROOM_ID)
        self.assertEqual(channel.name, ROOM_ID)

    def test_an_event_without_a_source_is_ignored(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["source"] = {}
        self.assertFalse(self.receive(event))

    def test_unsupported_event_is_ignored(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["type"] = "videoPlayComplete"
        self.assertFalse(self.receive(event))

    def test_an_event_without_a_date_still_gets_one(self):
        event = json.loads(json.dumps(self.text_event))
        event["events"][0].pop("timestamp")
        channel = self.receive(event)
        self.assertTrue(channel.message_ids.date)

    def test_the_message_carries_its_line_identifier(self):
        channel = self.receive(self.text_event)
        self.assertEqual(
            channel.message_ids.message_id,
            f"<325708@line-gateway-{self.gateway.id}>",
        )

    def test_a_quoted_message_is_linked(self):
        channel = self.receive(self.text_event)
        first = channel.message_ids
        event = json.loads(json.dumps(self.text_event))
        event["events"][0]["webhookEventId"] = "01FZ74A0TDDPYRVKNK77XKC3ZS"
        event["events"][0]["message"] = {
            "id": "325713",
            "type": "text",
            "text": "Still not sure",
            "quotedMessageId": "325708",
        }
        self.receive(event)
        answer = channel.message_ids.filtered(lambda m: "Still not sure" in m.body)
        self.assertEqual(answer.parent_id, first)

    def test_the_avatar_of_the_channel_is_the_line_icon(self):
        channel = self.receive(self.text_event)
        self.assertTrue(channel._generate_avatar())

    # ------------------------------------------------------------------
    # Not answering, and not answering twice
    # ------------------------------------------------------------------

    def test_receiving_never_answers(self):
        """Nothing is ever sent back to LINE without somebody writing it.

        The gateway has no automatic reply: the only outgoing call is the one
        triggered by a message posted in the channel.
        """
        self.integrate()
        with (
            patch("requests.get", side_effect=fake_get),
            patch("requests.post") as post_mock,
        ):
            self.post_event(self.text_event)
            self.post_event(self.image_event)
            self.post_event(self.postback_event)
            self.assertEqual(
                post_mock.call_count,
                0,
                "the gateway answered by itself: %s"
                % [call.kwargs.get("json") for call in post_mock.call_args_list],
            )
        self.assertTrue(self.channels().message_ids)

    def test_recording_an_event_never_answers_even_called_directly(self):
        """The flag is set where the message is posted, not only by the caller.

        The controller marks the request, but a module calling this on its own
        must not end up answering the customer either.
        """
        gateway = self.gateway.with_context(no_gateway_notification=False)
        event = self.text_event["events"][0]
        with (
            patch("requests.get", side_effect=fake_get),
            patch("requests.post") as post_mock,
        ):
            self.service._process_event(gateway, event)
            self.assertEqual(
                post_mock.call_count,
                0,
                "the received message was pushed back to its author: %s"
                % [call.kwargs.get("json") for call in post_mock.call_args_list],
            )
        self.assertTrue(self.channels().message_ids)

    @mute_logger(SERVICE_LOGGER)
    def test_a_redelivered_event_is_not_recorded_twice(self):
        channel = self.receive(self.text_event)
        self.assertEqual(len(channel.message_ids), 1)
        # LINE resends the very same event when it did not get a 2xx answer
        self.receive(self.text_event)
        self.assertEqual(len(channel.message_ids), 1)

    @mute_logger(SERVICE_LOGGER)
    def test_a_redelivered_postback_is_not_recorded_twice(self):
        channel = self.receive(self.postback_event)
        self.assertEqual(len(channel.message_ids), 1)
        self.receive(self.postback_event)
        self.assertEqual(len(channel.message_ids), 1)


class TestSend(LineCase):
    def test_send(self):
        channel = self.receive(self.text_event)
        with patch("requests.post", side_effect=fake_push) as post_mock:
            message = channel.message_post(
                body="It only needs more light",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
            self.assertEqual(post_mock.call_count, 1)
            payload = post_mock.call_args.kwargs["json"]
            headers = post_mock.call_args.kwargs["headers"]
        self.assertEqual(payload["to"], USER_ID)
        self.assertEqual(payload["messages"][0]["type"], "text")
        self.assertEqual(payload["messages"][0]["text"], "It only needs more light")
        self.assertIn("X-Line-Retry-Key", headers)
        notification = message.notification_ids
        self.assertEqual(notification.notification_status, "sent")
        self.assertEqual(notification.gateway_message_id, SENT_MESSAGE_ID)

    @mute_logger(SERVICE_LOGGER)
    def test_send_failure_is_recorded(self):
        channel = self.receive(self.text_event)
        with patch("requests.post") as post_mock:
            post_mock.return_value = FakeResponse(status_code=400)
            message = channel.message_post(
                body="It only needs more light",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        notification = message.notification_ids
        self.assertEqual(notification.notification_status, "exception")
        self.assertEqual(notification.failure_type, "unknown")

    @mute_logger(SERVICE_LOGGER)
    def test_a_message_with_nothing_to_send_is_not_left_pending(self):
        """An empty body is a failure the sender has to see."""
        channel = self.receive(self.text_event)
        with patch("requests.post", side_effect=fake_push) as post_mock:
            message = channel.message_post(
                body="", message_type="comment", subtype_xmlid="mail.mt_comment"
            )
            self.assertEqual(post_mock.call_count, 0)
        notification = message.notification_ids
        self.assertEqual(notification.notification_status, "exception")
        self.assertTrue(notification.failure_reason)

    @mute_logger(SERVICE_LOGGER)
    def test_an_answer_that_is_not_json_is_still_sent(self):
        channel = self.receive(self.text_event)

        def broken(url, **kwargs):
            response = FakeResponse()
            response.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
            return response

        with patch("requests.post", side_effect=broken):
            message = channel.message_post(
                body="It only needs more light",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        self.assertEqual(message.notification_ids.notification_status, "sent")

    def test_a_long_answer_is_split_and_not_cut(self):
        channel = self.receive(self.text_event)
        body = "A" * 12000
        with patch("requests.post", side_effect=fake_push) as post_mock:
            channel.message_post(
                body=body, message_type="comment", subtype_xmlid="mail.mt_comment"
            )
            payload = post_mock.call_args.kwargs["json"]
        texts = [message["text"] for message in payload["messages"]]
        self.assertEqual(len(texts), 3)
        self.assertEqual(sum(len(text) for text in texts), 12000)
        self.assertTrue(
            all(len(text) <= mail_gateway_line.MAX_TEXT_LENGTH for text in texts)
        )

    def test_send_attachment_as_a_picture(self):
        channel = self.receive(self.text_event)
        attachment = self.env["ir.attachment"].create(
            {"name": "lot.png", "raw": b"image", "mimetype": "image/png"}
        )
        with patch("requests.post", side_effect=fake_push) as post_mock:
            channel.message_post(
                body="Here it is",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                attachment_ids=attachment.ids,
            )
            payload = post_mock.call_args.kwargs["json"]
        types = [message["type"] for message in payload["messages"]]
        self.assertEqual(types, ["text", "image"])
        image = payload["messages"][1]
        self.assertIn("access_token=", image["originalContentUrl"])
        self.assertTrue(image["originalContentUrl"].startswith(BASE_URL))

    def test_send_other_attachment_as_a_link(self):
        channel = self.receive(self.text_event)
        attachment = self.env["ir.attachment"].create(
            {"name": "care.pdf", "raw": b"pdf", "mimetype": "application/pdf"}
        )
        with patch("requests.post", side_effect=fake_push) as post_mock:
            channel.message_post(
                body="Here it is",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                attachment_ids=attachment.ids,
            )
            payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][1]["type"], "text")
        self.assertIn("care.pdf", payload["messages"][1]["text"])

    @mute_logger(SERVICE_LOGGER)
    def test_an_attachment_needs_a_public_https_address(self):
        """LINE fetches the file itself: a wrong base url must not be silent."""
        channel = self.receive(self.text_event)
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "http://localhost:8069"
        )
        attachment = self.env["ir.attachment"].create(
            {"name": "lot.png", "raw": b"image", "mimetype": "image/png"}
        )
        with patch("requests.post", side_effect=fake_push) as post_mock:
            message = channel.message_post(
                body="Here it is",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                attachment_ids=attachment.ids,
            )
            self.assertEqual(post_mock.call_count, 0)
        notification = message.notification_ids
        self.assertEqual(notification.notification_status, "exception")
        self.assertIn("HTTPS", notification.failure_reason)

    def test_push_is_split_in_chunks_of_five(self):
        messages = [{"type": "text", "text": str(index)} for index in range(7)]
        with patch("requests.post", side_effect=fake_push) as post_mock:
            self.service._line_push(self.gateway, USER_ID, messages)
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(len(post_mock.call_args_list[0].kwargs["json"]["messages"]), 5)
        self.assertEqual(len(post_mock.call_args_list[1].kwargs["json"]["messages"]), 2)

    def test_push_without_a_conversation_is_refused(self):
        with self.assertRaises(UserError):
            self.service._line_push(
                self.gateway, False, [{"type": "text", "text": "x"}]
            )

    def test_push_without_a_message_is_refused(self):
        with self.assertRaises(UserError):
            self.service._line_push(self.gateway, USER_ID, [])

    def test_the_retry_key_is_the_same_on_every_attempt(self):
        messages = [{"type": "text", "text": str(index)} for index in range(7)]
        with patch("requests.post", side_effect=fake_push) as post_mock:
            self.service._line_push(
                self.gateway, USER_ID, messages, retry_key="notification-1"
            )
            self.service._line_push(
                self.gateway, USER_ID, messages, retry_key="notification-1"
            )
        keys = [
            call.kwargs["headers"]["X-Line-Retry-Key"]
            for call in post_mock.call_args_list
        ]
        self.assertEqual(keys[:2], keys[2:])
        # One key per chunk, so a chunk that already went through is known
        self.assertNotEqual(keys[0], keys[1])

    def test_the_retry_key_of_a_notification_follows_its_content(self):
        """A changed answer must not be refused as a duplicate of the old one."""
        notification = self.env["mail.notification"]
        first = self.service._line_notification_retry_key(
            notification, [{"type": "text", "text": "a"}]
        )
        again = self.service._line_notification_retry_key(
            notification, [{"type": "text", "text": "a"}]
        )
        other = self.service._line_notification_retry_key(
            notification, [{"type": "text", "text": "b"}]
        )
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_a_chunk_already_accepted_is_not_sent_twice(self):
        """LINE answers 409 on a retry key it has already seen."""
        with patch("requests.post") as post_mock:
            post_mock.return_value = FakeResponse(status_code=409)
            result = self.service._line_push(
                self.gateway,
                USER_ID,
                [{"type": "text", "text": "x"}],
                retry_key="notification-1",
            )
        self.assertEqual(result, {"sentMessages": []})

    def test_a_conflict_without_a_retry_key_is_an_error(self):
        with patch("requests.post") as post_mock:
            post_mock.return_value = FakeResponse(status_code=409)
            with self.assertRaises(requests.exceptions.HTTPError):
                self.service._line_push(
                    self.gateway, USER_ID, [{"type": "text", "text": "x"}]
                )

    def test_a_chunk_failing_halfway_keeps_the_keys_of_the_first_ones(self):
        messages = [{"type": "text", "text": str(index)} for index in range(7)]
        calls = []

        def half_broken(url, **kwargs):
            calls.append(kwargs["headers"]["X-Line-Retry-Key"])
            if len(calls) == 2:
                return FakeResponse(status_code=500)
            return fake_push(url, **kwargs)

        with patch("requests.post", side_effect=half_broken):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.service._line_push(
                    self.gateway, USER_ID, messages, retry_key="notification-1"
                )
            first_attempt = list(calls)
            self.service._line_push(
                self.gateway, USER_ID, messages, retry_key="notification-1"
            )
        # The chunk LINE already accepted carries the same key, so it is
        # discarded instead of being delivered a second time.
        self.assertEqual(calls[2:], first_attempt)

    def test_a_rate_limit_is_reported_as_an_error(self):
        for status_code in (429, 500, 502, 503, 504):
            with self.subTest(status_code=status_code):
                with self.assertRaises(requests.exceptions.HTTPError):
                    self.service._line_check_response(
                        FakeResponse(status_code=status_code)
                    )

    def test_an_unreachable_api_is_not_swallowed(self):
        with patch("requests.post") as post_mock:
            post_mock.side_effect = requests.exceptions.ConnectionError("boom")
            with self.assertRaises(requests.exceptions.ConnectionError):
                self.service._line_push(
                    self.gateway, USER_ID, [{"type": "text", "text": "x"}]
                )


class TestBuilders(LineCase):
    def columns(self, count, actions=1):
        return [
            {
                "title": "A" * 60,
                "text": "B" * 80,
                "thumbnail_url": "https://example.com/1.png",
                "actions": [
                    self.service._prepare_postback_action("Choose", f"lot={index}")
                    for index in range(actions)
                ],
            }
            for index in range(count)
        ]

    def test_postback_action(self):
        action = self.service._prepare_postback_action(
            "A very long label that LINE refuses", "lot=1", "The first one"
        )
        self.assertEqual(action["type"], "postback")
        self.assertEqual(action["data"], "lot=1")
        self.assertEqual(action["displayText"], "The first one")
        self.assertEqual(len(action["label"]), 20)

    def test_carousel_builder(self):
        carousel = self.service._prepare_carousel_message(
            self.columns(10), alt_text="Which one?"
        )
        self.assertEqual(carousel["type"], "template")
        self.assertEqual(carousel["altText"], "Which one?")
        columns = carousel["template"]["columns"]
        # LINE accepts a title of 40 and a text of 60 characters
        self.assertEqual(len(columns), 10)
        self.assertEqual(len(columns[0]["title"]), 40)
        self.assertEqual(len(columns[0]["text"]), 60)
        self.assertEqual(columns[0]["thumbnailImageUrl"], "https://example.com/1.png")

    def test_a_carousel_never_hides_a_column(self):
        """Cutting the list would hide items their owner has to be able to pick."""
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message(self.columns(11))

    def test_carousel_messages_are_paginated(self):
        messages = self.service._prepare_carousel_messages(
            self.columns(23), alt_text="Which one?"
        )
        self.assertEqual(len(messages), 3)
        self.assertEqual(
            [len(message["template"]["columns"]) for message in messages], [10, 10, 3]
        )

    def test_a_carousel_needs_columns(self):
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message([])

    def test_a_carousel_needs_an_action_on_every_column(self):
        columns = self.columns(2)
        columns[1]["actions"] = []
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message(columns)

    def test_a_carousel_needs_the_same_number_of_actions(self):
        columns = self.columns(2) + self.columns(1, actions=2)
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message(columns)

    def test_a_carousel_holds_at_most_three_actions(self):
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message(self.columns(1, actions=4))

    def test_a_carousel_thumbnail_has_to_be_https(self):
        columns = self.columns(1)
        columns[0]["thumbnail_url"] = "http://example.com/1.png"
        with self.assertRaises(UserError):
            self.service._prepare_carousel_message(columns)
