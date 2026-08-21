"""Invite portal: the friend-facing, mTLS-fronted half of #442.

Everything under ``/invite/*`` is served as server-rendered HTML and is the ONLY
surface nginx exposes on the public client-cert vhost. It carries no admin data
and no admin JavaScript.
"""
