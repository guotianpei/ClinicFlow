"""Eligibility staff-facing labels (E3a: reasons). SYNTHETIC ONLY.

OD-EL8 (Rachel, 2026-11-18), byte-exact. Staff-facing only; never patient-facing (EL-11).
`coverage_active` and the note-only reasons have no staff label. The closing-outcome and
contact-entry labels ship with E3b.
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
