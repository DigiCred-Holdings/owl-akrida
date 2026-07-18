"""Storage categories used by the AnonCreds Holder + Issuer.

Values must mirror ACA-Py's category strings so existing anoncreds
routing finds records via the same record_type.
"""

from __future__ import annotations

# Issuer-side (matches acapy_agent.indy.constants and
# acapy_agent.anoncreds.constants).
CATEGORY_SCHEMA = "schema"
CATEGORY_CRED_DEF = "credential_def"
CATEGORY_CRED_DEF_PRIVATE = "credential_def_private"
CATEGORY_CRED_DEF_KEY_PROOF = "credential_def_key_proof"
CATEGORY_REV_REG_DEF = "revocation_reg_def"
CATEGORY_REV_REG_DEF_PRIVATE = "revocation_reg_def_private"
# Legacy indy_credx revocation registry (acapy_agent.indy.constants).
CATEGORY_REV_REG = "revocation_reg"
CATEGORY_REV_REG_INFO = "revocation_reg_info"
# Newer anoncreds revocation list (acapy_agent.anoncreds.constants).
CATEGORY_REV_LIST = "revocation_list"

# Holder-side (matches acapy_agent.indy.credx.holder /
# acapy_agent.anoncreds.holder).
CATEGORY_LINK_SECRET = "master_secret"
LINK_SECRET_ID = "default"
CATEGORY_CREDENTIAL = "credential"
CATEGORY_MIME_TYPES = "attribute-mime-types"
CATEGORY_REV_STATE = "revocation_state"
