This module lets Odoo receive and answer messages from a
[LINE](https://line.me) official account, through the LINE Messaging API,
using the conversation model of `mail_gateway`.

Messages sent to the official account become messages of a Discuss channel:
pictures, videos, files, locations and stickers are stored along with the
text, and the sender is matched with a partner as soon as their LINE account
is linked to one. Answers written in that channel are pushed back to LINE.

Every request is verified against the channel secret, and an event LINE
delivers twice is recorded once.
