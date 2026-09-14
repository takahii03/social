A user writing to the official account opens a channel in Discuss, named
after their LINE display name. Everything they send afterwards lands in that
channel, and what the members of the gateway write there is pushed back to
them.

Nothing is ever sent to LINE on its own: the only outgoing call is the one a
message posted in the channel makes. A received event is recorded and stops
there.

The author of an incoming message is the partner whose **Gateway Channels**
hold the LINE user id of the sender. Until such a link exists the messages
are attributed to a guest, and the link can be created from the partner form.

An event LINE delivers twice, which happens when it did not get an answer in
time, is recorded once: received messages carry their LINE identifier in
`message_id`.

Modules building on this one can send message objects of their own with
`gateway._line_push(token, messages)`. Pass `retry_key` with any stable
string identifying what is being sent, and sending it again after a timeout
will not deliver it twice.

Two builders are provided for the common case of asking the user to pick
something:

```python
service = env["mail.gateway.line"]
columns = [
    {
        "title": product.name,
        "text": lot.name,
        "thumbnail_url": url,
        "actions": [service._prepare_postback_action("Choose", f"lot={lot.id}")],
    }
    for lot in lots
]
gateway._line_push(
    token,
    service._prepare_carousel_messages(columns, alt_text="Which one?"),
    retry_key=f"choose-lot-{ticket.id}",
)
```

A carousel holds ten columns, so `_prepare_carousel_messages` splits a longer
list over several of them rather than hiding the rest. Every column has to
carry the same number of actions and a thumbnail served over HTTPS, which is
what LINE requires; `_prepare_carousel_message` refuses anything else instead
of letting the API reject the message.

The answer comes back as a `postback` event and is recorded in the channel as
a note. Override `_process_event_postback` to act on it.
