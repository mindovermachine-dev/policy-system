"""HTTP/REST API layer for PS Service, mounted into ``ps_service.main.create_app``.

This REST layer binds loopback-only and is unauthenticated. Loopback-only binding is the
interim access control. Remote (non-loopback) transport, authentication, authorization and rate
limiting are out of scope for this package; remote MCP transport and authentication are hosted
in this same process.
"""
