"""L-1S package S-F: patient reply text -> reply class, and a synthetic intake adapter.

Owner decision Q-SF1 (2026-11-07, 10:58 AM ET): the platform reads patient replies with
the Q-B3 rules approved for the demonstrator (2026-10-25), unchanged:

- `1`, `YES`, `Y` -> confirm; `2` -> reschedule; `3`, `CANCEL` -> cancel (a cancel
  REQUEST: a staff follow-up item, never an automatic EHR change);
- `STOP`, `STOPALL`, `UNSUBSCRIBE`, `END`, `QUIT` -> stop (opt-out on the number, no
  staff item, D-S2);
- anything else -> unclear (staff item), including `NO`, `START`, empty text, extra
  words and punctuation.

Normalisation is exactly the demonstrator's: Python `text.strip().upper()` followed by a
whole-string lookup. Platform addition: text longer than `MAX_TEXT` characters is
unclear, checked before normalisation, so it can never be truncated into a command.

`ReplyIntake.receive` classifies and hands an `InboundCommand` to
`IntakeService.register_inbound` under the caller's trusted context (the tenant is never
inferred from the phone or the text). The raw text is never stored, logged, or put in an
exception; `InboundMessage` keeps it out of its repr. No provider payload integrity or
authentication is claimed (that belongs to a later ingress route). Not composed: nothing
in the platform, startup or composition modules imports this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from haloflow.m01.context import TenantContext
from haloflow.m05.intake import InboundCommand, InboundResult, IntakeService

MAX_TEXT: Final = 1600
UNCLEAR: Final = "unclear"
REPLY_TOKENS: Final[Mapping[str, str]] = MappingProxyType({
    "1": "confirm", "YES": "confirm", "Y": "confirm",
    "2": "reschedule",
    "3": "cancel", "CANCEL": "cancel",
    "STOP": "stop", "STOPALL": "stop", "UNSUBSCRIBE": "stop", "END": "stop", "QUIT": "stop",
})


class ReplyRefused(Exception):
    """The reply text is not acceptable. Carries only a fixed code, never the text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def classify(text: str) -> str:
    if type(text) is not str:
        raise ReplyRefused("TEXT_INVALID")
    if len(text) > MAX_TEXT:
        return UNCLEAR
    return REPLY_TOKENS.get(text.strip().upper(), UNCLEAR)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One inbound SMS as supplied by a trusted caller; `text` is never in its repr."""

    channel: str
    provider_message_id: str
    phone: str
    text: str = field(repr=False)


class ReplyIntake:
    def __init__(self, intake: IntakeService) -> None:
        self._intake = intake

    async def receive(self, context: TenantContext,
                      message: InboundMessage) -> InboundResult:
        reply_class = classify(message.text)
        command = InboundCommand(channel=message.channel,
                                 provider_message_id=message.provider_message_id,
                                 phone=message.phone, reply_class=reply_class)
        return await self._intake.register_inbound(context, command)
