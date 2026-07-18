"""KanonSecretsAdapter — SecretsManager for ACA-Py's DIDComm v2 layer."""

from __future__ import annotations

import logging
from typing import Optional

from acapy_agent.core.profile import ProfileSession
from acapy_agent.wallet.error import WalletNotFoundError
from didcomm_messaging import SecretsManager
from didcomm_messaging.crypto.backend.askar import AskarSecretKey

LOGGER = logging.getLogger(__name__)


class KanonSecretsAdapter(SecretsManager[AskarSecretKey]):
    """Resolve `kid` → `AskarSecretKey` from our keystore."""

    def __init__(self, session: ProfileSession):
        self.session = session

    async def get_secret_by_kid(self, kid: str) -> Optional[AskarSecretKey]:
        # Lazy imports keep this module light when DIDComm v2 isn't enabled.
        from kanon_storage.v1_0.wallet import crypto
        from kanon_storage.v1_0.wallet.keystore import Keystore

        profile = self.session.profile
        keystore = Keystore(
            profile_id=profile.profile_id,
            master_key=profile.config.master_key,
            dialect=profile.config.dialect,
        )
        try:
            _verkey, secret, alg_name, _meta = await keystore.fetch_by_kid(
                self.session.sa_session, kid
            )
        except WalletNotFoundError:
            LOGGER.debug("kanon_storage: no key for kid=%s", kid)
            return None

        key = crypto.key_from_secret(alg_name, secret)
        return AskarSecretKey(key=key, kid=kid)
