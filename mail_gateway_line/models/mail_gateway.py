# Copyright 2026 Takahiro Sunaga
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models

DEFAULT_API_URL = "https://api.line.me"
DEFAULT_DATA_API_URL = "https://api-data.line.me"
DEFAULT_TIMEOUT = 10
# Downloading a picture or a file a user sent takes longer than an API call,
# but the webhook waits for it, so this cannot be generous either.
DEFAULT_CONTENT_TIMEOUT = 30


class MailGateway(models.Model):
    _inherit = "mail.gateway"

    gateway_type = fields.Selection(
        selection_add=[("line", "LINE")], ondelete={"line": "cascade"}
    )
    line_api_url = fields.Char(
        default=DEFAULT_API_URL,
        help="Base URL of the LINE Messaging API.",
    )
    line_data_api_url = fields.Char(
        default=DEFAULT_DATA_API_URL,
        help="Base URL used to download the content of the received messages. "
        "LINE serves the message content from a different host than the API.",
    )
    line_download_content = fields.Boolean(
        default=True,
        help="Download the images, videos, audios and files sent by the user "
        "and store them as attachments of the received message.",
    )

    def _line_headers(self):
        """Authorization header built from the channel access token."""
        self.ensure_one()
        return {"Authorization": f"Bearer {self.token}"}

    def _line_timeout(self):
        return DEFAULT_TIMEOUT

    def _line_content_timeout(self):
        return DEFAULT_CONTENT_TIMEOUT

    def _can_set_webhook(self):
        self.ensure_one()
        if self.gateway_type == "line":
            # The channel secret is used to check the signature of every
            # incoming request, so we refuse to integrate without it.
            return bool(
                self.webhook_key
                and self.webhook_user_id
                and self.token
                and self.webhook_secret
            )
        return super()._can_set_webhook()

    @api.depends("token", "webhook_key", "webhook_secret", "webhook_user_id")
    def _compute_webhook_checks(self):
        return super()._compute_webhook_checks()

    def _line_push(self, token, messages, retry_key=False):
        """Send prepared LINE message objects to a user, group or room.

        Exposed on the gateway so that modules building their own message
        objects (carousels, flex messages...) do not need to know about the
        service model. ``retry_key`` is any stable string identifying what is
        being sent: passing one makes sending the same thing twice safe.
        """
        self.ensure_one()
        return self.env["mail.gateway.line"]._line_push(
            self, token, messages, retry_key=retry_key
        )
