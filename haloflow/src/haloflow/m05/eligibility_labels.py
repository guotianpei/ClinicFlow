"""Eligibility staff-facing labels. SYNTHETIC ONLY.

E3a: the reasons, OD-EL8 (Rachel, 2026-11-18), byte-exact. Staff-facing only; never
patient-facing (EL-11). `coverage_active` and the note-only reasons have no staff label.
E3b: the closing outcomes (OD-EL6) and the non-closing contact entries (OD-EL7), byte-exact;
`el_other` is the only outcome that requires a note (protected-note path, OD-D2).
"""

from types import MappingProxyType
from typing import Final

REASON_LABELS: Final = MappingProxyType({
    "coverage_inactive": "Payer reports coverage not active",
    "subscriber_not_found": "Payer could not find the member",
    "name_dob_mismatch": "Name or date of birth does not match payer records",
    "member_id_not_recognized": "Member ID not recognized by payer",
    "service_type_unsupported": "Payer could not answer for this service type",
    "payer_unavailable": "Payer did not respond (tried twice)",
    "response_unreadable": "Payer response could not be read",
    "nonpriority": "Payer not set up for automatic checks",
    "no_coverage": "No insurance on file",
    "incomplete": "Insurance details incomplete",
    "coverage_not_received": "Insurance not received from EHR",
})

OUTCOME_LABELS: Final = MappingProxyType({
    "el_verified_active": "Verified active by phone or payer portal",
    "el_confirmed_inactive": "Confirmed not active",
    "el_insurance_updated": "Insurance updated in EHR",
    "el_self_pay": "Patient will self-pay",
    "el_appt_cancelled": "No action \u2013 appointment cancelled",
    "el_other": "Other",
})

CONTACT_LABELS: Final = MappingProxyType({
    "el_left_message": "Left message for patient",
    "el_called_no_answer": "Called patient \u2013 no answer",
    "el_spoke_followup": "Spoke with patient \u2013 follow-up needed",
    "el_payer_awaiting": "Contacted payer \u2013 awaiting answer",
})
