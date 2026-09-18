"""Real-time chat: one server function, one channel, no polling.

Run it:  jongo dev examples/patterns/06_live_chat.py --port 8775
         Open it in two windows and type in one.

What to notice
--------------
* `@app.channel` declares who may subscribe. A channel that is not declared cannot be
  subscribed to at all — the same rule that keeps undecorated functions off the RPC boundary.
* `broadcast()` pushes to every subscribed browser; `live()` receives in the component.
* The sender gets the message through the same broadcast as everyone else, so there is
  one code path and no duplicate-message special case.
"""

from jongo import Jongo, broadcast, component, db, live, server, state
from jongo.html import button, div, form, h1, input_, li, p, strong, ul

app = Jongo(__name__, title="Chat")
ROOM = "room:lobby"


class Message(db.Model):
    who = db.Text(max_length=40)
    text = db.Text(max_length=500)
    sent = db.DateTime(auto_now_add=True)


@app.channel(ROOM)
def lobby(request):
    """Anyone may listen to the lobby. Return False here to refuse a subscription."""
    return True


@server
def say(request, who: str, text: str) -> dict:
    message = Message.create(who=who or "anon", text=text).to_dict()
    broadcast(ROOM, message)          # -> every browser subscribed to the lobby
    return {"sent": True}


@component
def Chat(history, who):
    messages = state(history)
    draft = state("")
    name = state(who)

    # The subscription opens when this component mounts and closes when it unmounts.
    live(ROOM, lambda message: messages.set([*messages.value, message]))

    async def send(event):
        text = draft.value.strip()
        if not text:
            return
        draft.set("")
        await say(name.value, text)   # the message arrives back over the channel

    return div(
        ul([li(strong(m["who"] + ": "), m["text"], key=m["id"]) for m in messages.value]),
        form(
            input_(value=name.value, placeholder="your name",
                   on_input=lambda e: name.set(e.target.value)),
            input_(value=draft.value, placeholder="message",
                   on_input=lambda e: draft.set(e.target.value)),
            button("Send"),
            on_submit=send,
        ),
        p("Open this page in two windows.", style={"opacity": 0.6}),
    )


@app.page("/")
def home():
    history = [m.to_dict() for m in Message.all().order_by("id")[:50]]
    return div(h1("Lobby"), Chat(history=history, who=""))
