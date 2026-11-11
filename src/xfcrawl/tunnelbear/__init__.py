"""Using a TunnelBear account as an HTTPS proxy, the way TunnelBear's browser extension (v4.5.0) does.

No OpenVPN, no root, no system-wide change: only what is sent through the proxy is tunnelled.

1. Log in: ``POST <dashboard>/v2/token`` with username, password, ``grant_type: password`` and a random
   device id, giving an access and a refresh token (or an MFA challenge).
2. Exchange the access token for a PolarBear token, the proxy back end: ``POST <polarbear>/auth``; the
   token comes back in the Authorization header.
3. ``GET <polarbear>/user`` gives the VPN token, ``/countries`` the countries, and
   ``/vpns/countries/<ISO>`` the proxy servers for one.
4. Use ``https://<server>:8080`` as an HTTPS proxy, with ``Proxy-Authorization: Basic <vpn token>:<vpn token>``.

This is TunnelBear's private API, not a published one: it can change with any extension update, and using
it may conflict with TunnelBear's terms. Requests count against the account's data allowance.
"""
