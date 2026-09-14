# Copyright 2026 Takahiro Sunaga
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import uuid
from datetime import datetime, timezone

import requests
from markupsafe import Markup

from odoo import models
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools.mail import html2plaintext, plaintext2html

from odoo.addons.base.models.ir_mail_server import MailDeliveryException

_logger = logging.getLogger(__name__)

# https://developers.line.biz/en/reference/messaging-api/#send-push-message
MAX_MESSAGES_PER_REQUEST = 5
# https://developers.line.biz/en/reference/messaging-api/#text-message
MAX_TEXT_LENGTH = 5000
# https://developers.line.biz/en/reference/messaging-api/#carousel
MAX_CAROUSEL_COLUMNS = 10
MAX_CAROUSEL_ACTIONS = 3
# LINE fetches image messages over HTTPS and only accepts these formats.
IMAGE_MIMETYPES = ("image/jpeg", "image/png")
# The content of a message is not kept forever: once LINE answers one of
# these, no retry brings the picture back.
CONTENT_GONE_STATUS_CODES = (404, 410)
# Answer of the push endpoint when the same X-Line-Retry-Key was already
# accepted: the messages are delivered, this is not a failure.
HTTP_CONFLICT = 409
# Namespace turning a retry key into the UUID the API expects.
RETRY_KEY_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


class MailGatewayLineService(models.AbstractModel):
    _inherit = "mail.gateway.abstract"
    _name = "mail.gateway.line"
    _description = "LINE Gateway services"

    # ------------------------------------------------------------------
    # Low level API access
    # ------------------------------------------------------------------

    def _line_check_response(self, response, accepted_status_codes=()):
        """Raise on anything the caller did not say it expects.

        Nothing retries by itself. An error raised while a webhook is
        being handled reaches the controller, which then answers
        something else than 2xx, and LINE sends the event again.
        """
        if response.status_code in accepted_status_codes:
            return response
        response.raise_for_status()
        return response

    def _line_response_json(self, response):
        """Body of an answer, tolerating a body that is not JSON.

        The API is documented to answer JSON, and a proxy answering something
        else must not turn a delivered message into a failed one.
        """
        try:
            return response.json() or {}
        except ValueError:
            _logger.warning(
                "LINE answered %s with a body that is not JSON", response.status_code
            )
            return {}

    def _line_get(self, gateway, url, timeout=None, accepted_status_codes=(), **kwargs):
        response = requests.get(
            url,
            headers=gateway._line_headers(),
            timeout=timeout or gateway._line_timeout(),
            **kwargs,
        )
        return self._line_check_response(
            response, accepted_status_codes=accepted_status_codes
        )

    def _line_post(
        self, gateway, url, headers=None, accepted_status_codes=(), **kwargs
    ):
        request_headers = gateway._line_headers()
        if headers:
            request_headers.update(headers)
        response = requests.post(
            url,
            headers=request_headers,
            timeout=gateway._line_timeout(),
            **kwargs,
        )
        return self._line_check_response(
            response, accepted_status_codes=accepted_status_codes
        )

    # ------------------------------------------------------------------
    # Webhook registration
    # ------------------------------------------------------------------

    def _set_webhook(self, gateway):
        """Register the webhook endpoint on the LINE channel."""
        url = f"{gateway.line_api_url}/v2/bot/channel/webhook/endpoint"
        try:
            response = requests.put(
                url,
                headers=gateway._line_headers(),
                json={"endpoint": gateway.webhook_url},
                timeout=gateway._line_timeout(),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            raise UserError(
                self.env._(
                    "The webhook endpoint could not be registered on LINE. "
                    "The URL has to be reachable from the internet over "
                    "HTTPS.\n%s",
                    err,
                )
            ) from err
        return super()._set_webhook(gateway)

    def _remove_webhook(self, gateway):
        # The Messaging API has no call to unset the endpoint, so it stays
        # registered on LINE and only the local state is cleared. Incoming
        # requests keep being rejected because the gateway is not integrated
        # anymore.
        _logger.info(
            "The webhook endpoint of %s stays registered on LINE, remove it "
            "from the LINE Developers console if needed.",
            gateway.name,
        )
        return super()._remove_webhook(gateway)

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    def _verify_update(self, bot_data, kwargs):
        """Check the X-Line-Signature header.

        The signature is the base64 encoded HMAC-SHA256 of the raw request
        body, keyed with the channel secret. A gateway without a channel
        secret cannot be verified, so its requests are rejected.
        """
        secret = bot_data.get("webhook_secret")
        signature = request.httprequest.headers.get("X-Line-Signature")
        if not secret or not signature:
            return False
        digest = hmac.new(
            secret.encode(), request.httprequest.data, hashlib.sha256
        ).digest()
        # Compared as bytes: a header holding anything else than ascii would
        # make the comparison of two strings raise instead of refusing.
        return hmac.compare_digest(
            base64.b64encode(digest), signature.encode("utf-8", "replace")
        )

    def _receive_update(self, gateway, update):
        """Record every event of the webhook payload.

        A failure is left to reach the controller on purpose: the answer
        is then not a 2xx and LINE sends the payload again, which is the
        only second chance an event gets. Receiving it twice is harmless,
        an event already recorded is recognised by its identifier.
        """
        if not isinstance(update, dict):
            _logger.warning("Ignoring a LINE payload that is not an object")
            return
        # Set on the gateway itself and not only on the service: the
        # channel is built from ``gateway.env``, and a channel without the
        # flag would push every received message straight back to its
        # author.
        gateway = gateway.with_context(no_gateway_notification=True)
        service = self.with_context(no_gateway_notification=True)
        for event in update.get("events") or []:
            if isinstance(event, dict):
                service._process_event(gateway, event)

    def _line_source_token(self, event):
        """Identifier of the conversation the event belongs to."""
        source = event.get("source") or {}
        return source.get("groupId") or source.get("roomId") or source.get("userId")

    def _line_event_date(self, event):
        timestamp = event.get("timestamp")
        if not timestamp:
            return False
        # LINE timestamps are in milliseconds.
        return datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc).replace(
            tzinfo=None
        )

    def _line_message_id(self, gateway, event):
        """Value stored in ``mail.message.message_id`` for a received event.

        It makes a redelivered event recognisable: LINE sends the same
        ``webhookEventId`` again when it did not get an answer in time, and an
        attacker replaying a signed body would otherwise create a copy of the
        message on every replay.
        """
        message_id = (event.get("message") or {}).get("id")
        if message_id:
            return self._line_message_id_from_token(gateway, message_id)
        event_id = event.get("webhookEventId")
        if event_id:
            return f"<event-{event_id}@line-gateway-{gateway.id}>"
        return False

    def _line_message_id_from_token(self, gateway, message_token):
        return f"<{message_token}@line-gateway-{gateway.id}>"

    def _line_find_message(self, message_id):
        if not message_id:
            return self.env["mail.message"]
        return (
            self.env["mail.message"]
            .sudo()
            .search([("message_id", "=", message_id)], limit=1)
        )

    def _process_event(self, gateway, event):
        event_type = event.get("type")
        handler = getattr(self, f"_process_event_{event_type}", None)
        if not handler:
            _logger.info("Ignoring unsupported LINE event type %s", event_type)
            return False
        return handler(gateway, event)

    def _process_event_message(self, gateway, event):
        token = self._line_source_token(event)
        if not token:
            return False
        channel = self._get_channel(gateway, token, event, force_create=True)
        if not channel:
            return False
        return self._process_update(channel, event)

    def _process_event_postback(self, gateway, event):
        """Record the answer given by tapping a template or a quick reply.

        The message is posted as a notification so that it is never sent back
        to LINE. Modules defining their own postback actions are expected to
        override this method to act on ``event["postback"]["data"]``.
        """
        token = self._line_source_token(event)
        if not token:
            return False
        channel = self._get_channel(gateway, token, event, force_create=True)
        if not channel:
            return False
        message_id = self._line_message_id(gateway, event)
        existing = self._line_find_message(message_id)
        if existing:
            _logger.info("The LINE event %s is already recorded", message_id)
            return existing
        postback = event.get("postback") or {}
        parts = [postback.get("data") or ""]
        parts += [
            f"{key}={value}"
            for key, value in sorted((postback.get("params") or {}).items())
        ]
        body = " ".join(part for part in parts if part)
        if not body:
            return False
        author = self._get_author(gateway, event)
        values = {
            "body": plaintext2html(body),
            "author_id": author and author._name == "res.partner" and author.id,
            "gateway_type": "line",
            "subtype_xmlid": "mail.mt_note",
            "message_type": "notification",
        }
        values.update(self._line_message_post_values(gateway, event, message_id))
        return (
            channel.sudo()
            .with_context(no_gateway_notification=True)
            .message_post(**values)
        )

    def _process_event_follow(self, gateway, event):
        """A user added the official account: open the conversation."""
        token = self._line_source_token(event)
        if not token:
            return False
        return self._get_channel(gateway, token, event, force_create=True)

    def _line_message_post_values(self, gateway, event, message_id):
        """Common message values built from the event itself."""
        values = {}
        if message_id:
            values["message_id"] = message_id
        date = self._line_event_date(event)
        if date:
            # Only when known: passing False would store a message without a
            # date and break the ordering of the conversation.
            values["date"] = date
        return values

    def _process_update(self, channel, event):
        channel.ensure_one()
        gateway = channel.gateway_id
        message = event.get("message") or {}
        message_id = self._line_message_id(gateway, event)
        existing = self._line_find_message(message_id)
        if existing:
            _logger.info("The LINE message %s is already recorded", message_id)
            return existing
        body, attachments = self._line_message_content(gateway, message)
        if not body and not attachments:
            return False
        author = self._get_author(gateway, event)
        if author and author._name == "mail.guest":
            channel = channel.with_user(
                self.env.ref("base.public_user").id
            ).with_context(guest=author)
        values = {
            "body": body,
            "author_id": author and author._name == "res.partner" and author.id,
            "gateway_type": "line",
            "subtype_xmlid": "mail.mt_comment",
            "message_type": "comment",
            "attachments": attachments,
        }
        values.update(self._line_message_post_values(gateway, event, message_id))
        # The flag is set again here and not only on the gateway: this is the
        # single call that would otherwise answer the customer without anybody
        # writing the answer.
        new_message = (
            channel.sudo()
            .with_context(no_gateway_notification=True)
            .message_post(**values)
        )
        self._post_process_message(new_message, channel)
        self._line_link_quoted_message(channel, message, new_message)
        return new_message

    def _line_link_quoted_message(self, channel, message, new_message):
        """Link the received message to the one the user quoted, if any."""
        quoted_id = message.get("quotedMessageId")
        if not quoted_id:
            return False
        related_message = (
            self.env["mail.notification"]
            .sudo()
            .search(
                [
                    ("gateway_channel_id", "=", channel.id),
                    ("gateway_message_id", "=", quoted_id),
                ],
                limit=1,
            )
            .mail_message_id
        )
        if not related_message:
            # Not something we sent: the user quoted one of their own messages.
            related_message = self._line_find_message(
                self._line_message_id_from_token(channel.gateway_id, quoted_id)
            )
        if not related_message:
            return False
        new_message.parent_id = related_message.id
        return related_message

    def _line_message_content(self, gateway, message):
        """Return the ``(body, attachments)`` of a received message."""
        message_type = message.get("type")
        body = ""
        attachments = []
        if message_type == "text":
            body = plaintext2html(message.get("text") or "")
        elif message_type in ("image", "video", "audio", "file"):
            attachment = self._line_download_content(gateway, message)
            if attachment:
                attachments.append(attachment)
            else:
                # The content is gone or hosted elsewhere. The message is kept
                # anyway: an inquiry must never be dropped silently.
                body = plaintext2html(
                    self.env._(
                        "Received a %s that could not be downloaded", message_type
                    )
                )
        elif message_type == "location":
            body = self._line_location_body(message)
        elif message_type == "sticker":
            body = self._line_sticker_body(message)
        else:
            # Same reason: the content of a message type this module does not
            # know is unreadable, but the customer did write to the shop.
            _logger.info("Unsupported LINE message type %s", message_type)
            body = plaintext2html(
                self.env._(
                    "Received a message of the unsupported type %s",
                    message_type or "unknown",
                )
            )
        return body, attachments

    def _line_location_body(self, message):
        latitude = message.get("latitude")
        longitude = message.get("longitude")
        label = message.get("title") or message.get("address") or "Location"
        if latitude is None or longitude is None:
            return plaintext2html(label)
        url = f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
        return Markup(
            '<a target="_blank" rel="noreferrer noopener" href="%s">%s</a>'
        ) % (url, label)

    def _line_sticker_body(self, message):
        keywords = message.get("keywords") or []
        return plaintext2html(message.get("text") or ", ".join(keywords) or "Sticker")

    def _line_download_content(self, gateway, message):
        """Download the binary content of a received message.

        Returns the attachment tuple expected by ``message_post``, or
        ``False`` when the content cannot be downloaded.
        """
        if not gateway.line_download_content:
            return False
        message_id = message.get("id")
        if not message_id:
            return False
        if (message.get("contentProvider") or {}).get("type") == "external":
            # LINE does not host the file, there is nothing to download.
            return False
        url = f"{gateway.line_data_api_url}/v2/bot/message/{message_id}/content"
        response = self._line_get(
            gateway,
            url,
            timeout=gateway._line_content_timeout(),
            accepted_status_codes=CONTENT_GONE_STATUS_CODES,
        )
        if response.status_code in CONTENT_GONE_STATUS_CODES:
            # Nothing brings it back, and losing the picture is better than
            # losing the whole inquiry: the message is kept without it.
            _logger.warning(
                "The content of the LINE message %s is gone (%s), keeping "
                "the message without it",
                message_id,
                response.status_code,
            )
            return False
        mimetype = (response.headers.get("Content-Type") or "").split(";")[0]
        name = message.get("fileName")
        if not name:
            name = "{}{}".format(message_id, mimetypes.guess_extension(mimetype) or "")
        return (name, response.content, {})

    # ------------------------------------------------------------------
    # Author and channel identification
    # ------------------------------------------------------------------

    def _line_profile_url(self, gateway, event):
        source = event.get("source") or {}
        user_id = source.get("userId")
        if not user_id:
            return False
        if source.get("groupId"):
            return (
                f"{gateway.line_api_url}/v2/bot/group/"
                f"{source['groupId']}/member/{user_id}"
            )
        if source.get("roomId"):
            return (
                f"{gateway.line_api_url}/v2/bot/room/"
                f"{source['roomId']}/member/{user_id}"
            )
        return f"{gateway.line_api_url}/v2/bot/profile/{user_id}"

    def _line_get_profile(self, gateway, event):
        """Best effort profile lookup.

        A failure here must never discard the incoming message, so errors are
        swallowed and an empty profile is returned.
        """
        url = self._line_profile_url(gateway, event)
        if not url:
            return {}
        try:
            return self._line_get(gateway, url).json()
        except (requests.exceptions.RequestException, ValueError) as err:
            _logger.info("Could not read the LINE profile: %s", err)
            return {}

    def _get_author_vals(self, gateway, user_id, event):
        profile = self._line_get_profile(gateway, event)
        return {
            "name": profile.get("displayName") or "LINE user",
            "gateway_id": gateway.id,
            "gateway_token": str(user_id),
        }

    def _get_author(self, gateway, update):
        user_id = (update.get("source") or {}).get("userId")
        if not user_id:
            return super()._get_author(gateway, update)
        gateway_partner = self.env["res.partner.gateway.channel"].search(
            [("gateway_id", "=", gateway.id), ("gateway_token", "=", str(user_id))],
            limit=1,
        )
        if gateway_partner:
            return gateway_partner.partner_id
        guest = self.env["mail.guest"].search(
            [("gateway_id", "=", gateway.id), ("gateway_token", "=", str(user_id))],
            limit=1,
        )
        if guest:
            return guest
        return self.env["mail.guest"].create(
            self._get_author_vals(gateway, user_id, update)
        )

    def _line_channel_name(self, gateway, token, event):
        source = event.get("source") or {}
        if source.get("groupId"):
            try:
                summary = self._line_get(
                    gateway,
                    f"{gateway.line_api_url}/v2/bot/group/{source['groupId']}/summary",
                ).json()
                return summary.get("groupName") or token
            except (requests.exceptions.RequestException, ValueError) as err:
                _logger.info("Could not read the LINE group summary: %s", err)
                return token
        if source.get("roomId"):
            return token
        # The author of the event has just been resolved for this same user,
        # so its name is reused instead of asking the profile API again.
        author = self._get_author(gateway, event)
        return (author and author.display_name) or token

    def _get_channel_vals(self, gateway, token, update):
        result = super()._get_channel_vals(gateway, token, update)
        name = self._line_channel_name(gateway, token, update)
        result["name"] = name
        result["anonymous_name"] = name
        return result

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _line_retry_key(self, retry_key, index):
        """UUID identifying one request, stable across attempts."""
        return str(uuid.uuid5(RETRY_KEY_NAMESPACE, f"{retry_key}-{index}"))

    def _line_notification_retry_key(self, record, messages):
        """Key of an outgoing notification, stable for as long as it is the same.

        Sending the notification again reuses it, so what LINE already
        accepted is not delivered twice. It follows the content as well: were
        the answer to change, sending it would otherwise be refused as a
        duplicate and never reach the customer.
        """
        digest = hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode()
        ).hexdigest()
        return f"mail-notification-{record.id}-{digest[:16]}"

    def _line_push(self, gateway, token, messages, retry_key=False):
        """Push message objects to a conversation.

        LINE accepts at most 5 message objects per request, so the list is
        sent in chunks. When ``retry_key`` is given, every chunk carries a
        key derived from it: sending the same thing again, after a timeout or
        after a chunk failed halfway through, makes LINE discard what it had
        already accepted instead of delivering it twice.
        """
        if not token:
            raise UserError(self.env._("The LINE conversation is unknown"))
        if not messages:
            raise UserError(self.env._("There is nothing to send to LINE"))
        url = f"{gateway.line_api_url}/v2/bot/message/push"
        sent_messages = []
        for index in range(0, len(messages), MAX_MESSAGES_PER_REQUEST):
            chunk = messages[index : index + MAX_MESSAGES_PER_REQUEST]
            headers = {}
            accepted_status_codes = ()
            if retry_key:
                headers["X-Line-Retry-Key"] = self._line_retry_key(retry_key, index)
                accepted_status_codes = (HTTP_CONFLICT,)
            response = self._line_post(
                gateway,
                url,
                headers=headers,
                accepted_status_codes=accepted_status_codes,
                json={"to": token, "messages": chunk},
            )
            if response.status_code == HTTP_CONFLICT:
                # Already accepted with the same key on a previous attempt.
                continue
            sent_messages.extend(
                self._line_response_json(response).get("sentMessages") or []
            )
        return {"sentMessages": sent_messages}

    def _line_attachment_url(self, attachment):
        """Public URL of an attachment, used as content URL of a message."""
        attachment = attachment.sudo()
        access_token = attachment.access_token or attachment.generate_access_token()[0]
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        )
        if not base_url.startswith("https://"):
            # LINE downloads the file itself and simply shows nothing when it
            # cannot: refusing here turns a silent non-delivery into an error
            # the sender can see.
            raise UserError(
                self.env._(
                    "LINE downloads the attachments from this server over "
                    "HTTPS. Set the system parameter web.base.url to the "
                    "public HTTPS address of this instance before sending an "
                    "attachment."
                )
            )
        return f"{base_url}/web/content/{attachment.id}?access_token={access_token}"

    def _line_attachment_messages(self, gateway, attachment):
        """Message objects sending one attachment.

        LINE fetches the media itself over HTTPS instead of accepting an
        upload, and has no message type for arbitrary files, so anything that
        is not a supported image is sent as a link.
        """
        url = self._line_attachment_url(attachment)
        if attachment.mimetype in IMAGE_MIMETYPES:
            return [
                {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
            ]
        return [{"type": "text", "text": f"{attachment.name}\n{url}"}]

    def _prepare_messages(self, gateway, record):
        """Message objects of an outgoing notification."""
        messages = []
        body = self._get_message_body(record)
        if body:
            text = html2plaintext(body)
            # A text longer than what LINE accepts is split instead of being
            # cut: half an answer, marked as sent, is worse than two messages.
            for index in range(0, len(text), MAX_TEXT_LENGTH):
                messages.append(
                    {"type": "text", "text": text[index : index + MAX_TEXT_LENGTH]}
                )
        for attachment in record.mail_message_id.attachment_ids:
            messages.extend(self._line_attachment_messages(gateway, attachment))
        return messages

    def _send(
        self,
        gateway,
        record,
        auto_commit=False,
        raise_exception=False,
        parse_mode=False,
    ):
        try:
            messages = self._prepare_messages(gateway, record)
            if not messages:
                # Nothing was sent, so the notification must not stay in its
                # initial state: it would look like a message on its way.
                raise UserError(
                    self.env._("The message holds nothing that can be sent to LINE")
                )
            result = self._line_push(
                gateway,
                record.gateway_channel_id.gateway_channel_token,
                messages,
                retry_key=self._line_notification_retry_key(record, messages),
            )
        except Exception as exc:
            _logger.exception("Issue sending message with id %s", record.id)
            record.sudo().write(
                {
                    "notification_status": "exception",
                    "failure_reason": str(exc),
                    "failure_type": "unknown",
                }
            )
            if raise_exception:
                raise MailDeliveryException(
                    self.env._("Unable to send the LINE message")
                ) from exc
        else:
            sent_messages = result.get("sentMessages") or []
            record.sudo().write(
                {
                    "notification_status": "sent",
                    "failure_reason": False,
                    "failure_type": False,
                    "gateway_message_id": (
                        sent_messages and sent_messages[0].get("id") or False
                    ),
                }
            )
        if auto_commit is True:
            # pylint: disable=invalid-commit
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Message builders for the modules built on top of this one
    # ------------------------------------------------------------------

    def _prepare_postback_action(self, label, data, display_text=False):
        """Action replying with ``data`` when the user taps it.

        https://developers.line.biz/en/reference/messaging-api/#postback-action
        """
        action = {"type": "postback", "label": label[:20], "data": data}
        if display_text:
            action["displayText"] = display_text
        return action

    def _check_carousel_columns(self, columns):
        """Refuse a carousel LINE would reject, or one hiding half its columns."""
        if not columns:
            raise UserError(self.env._("A carousel needs at least one column"))
        if len(columns) > MAX_CAROUSEL_COLUMNS:
            raise UserError(
                self.env._(
                    "LINE shows at most %(maximum)s columns in a carousel and "
                    "%(count)s were given. Use _prepare_carousel_messages to "
                    "split them over several carousels.",
                    maximum=MAX_CAROUSEL_COLUMNS,
                    count=len(columns),
                )
            )
        counts = {len(column.get("actions") or []) for column in columns}
        if 0 in counts:
            raise UserError(
                self.env._("Every column of a carousel needs at least one action")
            )
        if len(counts) > 1:
            raise UserError(
                self.env._(
                    "Every column of a carousel needs the same number of actions"
                )
            )
        if max(counts) > MAX_CAROUSEL_ACTIONS:
            raise UserError(
                self.env._(
                    "A column of a carousel holds at most %s actions",
                    MAX_CAROUSEL_ACTIONS,
                )
            )
        for column in columns:
            thumbnail = column.get("thumbnail_url")
            if thumbnail and not thumbnail.startswith("https://"):
                raise UserError(
                    self.env._("LINE only loads a thumbnail served over HTTPS")
                )

    def _prepare_carousel_message(
        self,
        columns,
        alt_text=False,
        image_aspect_ratio="rectangle",
        image_size="cover",
    ):
        """Carousel of up to 10 columns the user picks one from.

        ``columns`` is a list of dictionaries with the keys ``title``,
        ``text``, ``thumbnail_url`` and ``actions``. Every column has to carry
        the same number of actions, which is what LINE requires. More than ten
        columns is refused rather than cut: use
        :meth:`_prepare_carousel_messages` to offer a longer list.

        https://developers.line.biz/en/reference/messaging-api/#carousel
        """
        self._check_carousel_columns(columns)
        line_columns = []
        for column in columns:
            line_column = {
                "text": (column.get("text") or " ")[:60],
                "actions": column["actions"],
            }
            if column.get("title"):
                line_column["title"] = column["title"][:40]
            if column.get("thumbnail_url"):
                line_column["thumbnailImageUrl"] = column["thumbnail_url"]
            line_columns.append(line_column)
        return {
            "type": "template",
            "altText": (alt_text or "Please choose")[:400],
            "template": {
                "type": "carousel",
                "columns": line_columns,
                "imageAspectRatio": image_aspect_ratio,
                "imageSize": image_size,
            },
        }

    def _prepare_carousel_messages(self, columns, **kwargs):
        """Split a list of columns over as many carousels as needed.

        Every column stays reachable: a customer owning more than ten plants
        has to be able to pick any of them.
        """
        return [
            self._prepare_carousel_message(
                columns[index : index + MAX_CAROUSEL_COLUMNS], **kwargs
            )
            for index in range(0, len(columns), MAX_CAROUSEL_COLUMNS)
        ]
