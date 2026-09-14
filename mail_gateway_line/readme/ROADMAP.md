- Outgoing attachments are limited by what the Messaging API accepts. LINE
  fetches the media itself over HTTPS instead of taking an upload, and it has
  no message type for arbitrary files, so only JPEG and PNG images are sent
  as pictures. Anything else is sent as a link to the attachment. Video and
  audio messages would additionally need a separate preview image and a
  duration, which Odoo does not know.
- Because LINE fetches the media itself, an attachment is sent as a link
  carrying its access token, and that link keeps working afterwards. Nothing
  expires it: on an instance where the pictures are confidential, plan how
  long those attachments stay reachable.
- Everything happens while the request that triggered it is being handled,
  like the other gateways of this repository. An answer is pushed while the
  message is being posted, and an event is recorded while the webhook is
  being answered, downloading the picture it carries along the way. A slow
  API therefore makes posting or answering the webhook slow.
- Nothing is retried here. A failure while sending is recorded on the
  notification, and a failure while receiving makes the webhook answer
  something else than a 2xx, which is what asks LINE to deliver the event
  again. That second chance exists only when **Webhook redelivery** is turned
  on for the channel, and LINE does not disclose how many times or how often
  it tries.
- Messages are sent with the push endpoint, which is counted against the
  monthly quota of the account. The reply endpoint, which is free, is not
  usable here because it expires shortly after the message is received, well
  before an answer is written.
- Removing the webhook only clears the state on the Odoo side. The Messaging
  API has no call to unset the endpoint, so it has to be removed from the
  LINE Developers console as well.
- Only the `message`, `postback` and `follow` events are handled. The other
  events of the Messaging API are ignored.
- A conversation is opened for every user writing to the account, so the
  `has_new_channel_security` flag of the gateway has no effect here: a
  message that opens no conversation would be a lost inquiry.
