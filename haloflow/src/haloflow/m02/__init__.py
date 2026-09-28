"""M02: event and operation foundation.

CP2-2a ships the lock owner role constant (`roles`) and the per-tenant
`operation_registry` migration unit (`units`). CP2-2b adds the typed lock gateway
unit (`units`), its installed-state profile (`gateway_profile`), the refusal codes
(`codes`) and the caller helper (`lock`). M02 depends on M01; M01 never imports
M02 (architecture v4 §2).
"""
