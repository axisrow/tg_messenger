"""Canonical user-facing strings shared by the three frontends.

The project deliberately uses one language instead of a runtime locale layer.
Keeping the strings that describe cross-frontend concepts here prevents the TUI,
web, and CLI from drifting apart again.
"""

READ_ONLY_MESSAGE = "This chat is read-only — you cannot send messages here."
MESSAGE_SENT = "Message sent"
REACTION_SENT = "Reaction sent"

