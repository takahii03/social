You need a Messaging API channel on the
[LINE Developers console](https://developers.line.biz/console/). An official
account that already exists can be linked to a Messaging API channel, there
is no need to open a new one.

Odoo has to be reachable from the internet over HTTPS. LINE refuses a webhook
endpoint that is not HTTPS, and it downloads the pictures you send from the
same address, so check that **Settings / Technical / System Parameters /
web.base.url** holds the public address of the instance. Sending an
attachment is refused while it is not an HTTPS address, because LINE would
simply show nothing.

Turn **Webhook redelivery** on for the channel as well. An event this
server could not record is only sent again when it is on, and an event
LINE delivers twice is recognised and recorded once.

To create the gateway, go to **Settings / Technical / Email / Gateway** and
create a record of type *LINE*:

1. Copy the **Channel secret** of the LINE channel into the **Webhook
   secret** field. It is the key used to check the signature of every
   incoming request, and the gateway refuses to work without it.
2. Issue a long lived **Channel access token** and copy it into the **Token**
   field.
3. Set a random value of your choice in the **Webhook Key** field. It is the
   part of the webhook URL identifying this gateway.
4. Add the users who should follow the conversations in the **Members** tab.
5. Save, then press **Integrate Webhook**. The endpoint is registered on the
   LINE channel and the state becomes *Integrated*.
6. Back on the LINE Developers console, in the **Messaging API** tab, turn
   **Use webhook** on. Turn **Auto-reply messages** and **Greeting messages**
   off if every answer should be written from Odoo.
