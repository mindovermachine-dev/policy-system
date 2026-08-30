"""HTTP/REST API layer for PS Service, mounted into ``ps_service.main.create_app``.

This REST layer binds loopback-only and is unauthenticated. Loopback-only
binding is the interim access control for the walking skeleton. Remote
(non-loopback) transport, authentication, authorization, and rate limiting are
out of scope here and tracked in mindovermachine-dev/policy-system#39 (sibling:
remote MCP transport + auth, hosted in this same process).
"""
