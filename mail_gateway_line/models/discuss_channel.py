# Copyright 2026 Takahiro Sunaga
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models
from odoo.tools.misc import file_path

# Brand colour of the LINE logo. The avatar of the other gateways is tinted
# with a colour derived from the channel, but a messenger is recognised by the
# colour of its mark, so it is kept the same on every conversation.
LINE_GREEN = "#06c755"


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    def _generate_avatar_gateway(self):
        if self.gateway_id.gateway_type == "line":
            path = file_path("mail_gateway_line/static/description/icon.svg")
            with open(path) as f:
                avatar = f.read()
            return avatar.replace("fill:#875a7b", f"fill:{LINE_GREEN}")
        return super()._generate_avatar_gateway()
