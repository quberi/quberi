"""External-system integrations (Feishu, DingTalk, WeCom, …).

Each provider ships as a subpackage with a common shape:

* ``client.py`` — thin HTTP wrapper with auth/token handling
* ``docs.py`` / ``contacts.py`` / ``messages.py`` — domain APIs

The top-level ``manager.py`` (arrives with the org-scoped install
flow) keeps track of which integrations each org has enabled.
"""
