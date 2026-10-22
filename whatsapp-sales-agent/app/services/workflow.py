"""Lead workflow state machine.

Happy path::

    NEW_LEAD -> VALIDATING -> READY_TO_CONTACT -> MESSAGE_SENT -> (DELIVERED)
      -> CONVERSATION_ACTIVE <-> QUALIFYING / AWAITING_CUSTOMER
      -> SUMMARY_PENDING -> SHEET_SYNC_PENDING -> CLOSED

Side states: INVALID_LEAD, CONSENT_REQUIRED, DUPLICATE_REVIEW, SKIPPED_EXISTING,
MESSAGE_FAILED, HUMAN_HANDOVER, DO_NOT_CONTACT, ERROR.

Every transition is validated and written to the audit log. Recovery after a
restart is driven by the persistent job queue (see ``message_queue.py``): a lead
is only ever moved forward by a job, and jobs are re-claimed after a crash.
"""

from __future__ import annotations

from app.models.lead import LeadState as S

ACTIVE_CONVERSATION_STATES = {
    S.MESSAGE_SENT, S.DELIVERED, S.CONVERSATION_ACTIVE, S.QUALIFYING, S.AWAITING_CUSTOMER,
}

_CONV = {S.CONVERSATION_ACTIVE, S.QUALIFYING, S.AWAITING_CUSTOMER}

TRANSITIONS: dict[S, set[S]] = {
    S.NEW_LEAD: {S.VALIDATING, S.SKIPPED_EXISTING, S.ERROR},
    S.VALIDATING: {S.READY_TO_CONTACT, S.INVALID_LEAD, S.CONSENT_REQUIRED,
                   S.DUPLICATE_REVIEW, S.DO_NOT_CONTACT, S.ERROR},
    # Operator fixes the data / consent / duplicate decision and re-validates.
    S.INVALID_LEAD: {S.VALIDATING},
    S.CONSENT_REQUIRED: {S.VALIDATING, S.DO_NOT_CONTACT},
    S.DUPLICATE_REVIEW: {S.VALIDATING, S.READY_TO_CONTACT, S.CLOSED},
    S.SKIPPED_EXISTING: {S.VALIDATING},
    S.READY_TO_CONTACT: {S.MESSAGE_SENT, S.MESSAGE_FAILED, S.DO_NOT_CONTACT, S.ERROR},
    S.MESSAGE_SENT: {S.DELIVERED, S.MESSAGE_FAILED, *_CONV, S.HUMAN_HANDOVER,
                     S.DO_NOT_CONTACT, S.SUMMARY_PENDING, S.ERROR},
    S.DELIVERED: {S.MESSAGE_FAILED, *_CONV, S.HUMAN_HANDOVER, S.DO_NOT_CONTACT,
                  S.SUMMARY_PENDING, S.ERROR},
    S.MESSAGE_FAILED: {S.READY_TO_CONTACT, S.ERROR, S.DO_NOT_CONTACT, S.CONVERSATION_ACTIVE},
    S.CONVERSATION_ACTIVE: {*_CONV, S.HUMAN_HANDOVER, S.DO_NOT_CONTACT,
                            S.SUMMARY_PENDING, S.ERROR},
    S.QUALIFYING: {*_CONV, S.HUMAN_HANDOVER, S.DO_NOT_CONTACT, S.SUMMARY_PENDING, S.ERROR},
    S.AWAITING_CUSTOMER: {*_CONV, S.HUMAN_HANDOVER, S.DO_NOT_CONTACT,
                          S.SUMMARY_PENDING, S.ERROR},
    S.HUMAN_HANDOVER: {S.CONVERSATION_ACTIVE, S.AWAITING_CUSTOMER, S.SUMMARY_PENDING,
                       S.DO_NOT_CONTACT, S.ERROR},
    S.SUMMARY_PENDING: {S.SHEET_SYNC_PENDING, S.CLOSED, S.CONVERSATION_ACTIVE,
                        S.HUMAN_HANDOVER, S.DO_NOT_CONTACT, S.ERROR},
    S.SHEET_SYNC_PENDING: {S.CLOSED, S.CONVERSATION_ACTIVE, S.HUMAN_HANDOVER,
                           S.DO_NOT_CONTACT, S.ERROR},
    # A new inbound message may reopen a closed conversation.
    S.CLOSED: {S.CONVERSATION_ACTIVE, S.HUMAN_HANDOVER, S.DO_NOT_CONTACT},
    # Do-not-contact is sticky; only an operator can lift it (re-validate).
    S.DO_NOT_CONTACT: {S.VALIDATING},
    S.ERROR: {S.READY_TO_CONTACT, S.VALIDATING, S.CONVERSATION_ACTIVE, S.SUMMARY_PENDING,
              S.HUMAN_HANDOVER, S.DO_NOT_CONTACT, S.CLOSED},
}


class InvalidTransition(Exception):
    pass


def can_transition(current: S | str, target: S | str) -> bool:
    current, target = S(current), S(target)
    if current == target:
        return True
    return target in TRANSITIONS.get(current, set())


def assert_transition(current: S | str, target: S | str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{S(current).value} -> {S(target).value} is not allowed")
