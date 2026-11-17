"""Routes added after the first release, one module per feature.

`main.py` holds the app, its middleware and the endpoints every installed
extension already calls. Each newer feature lives in its own router here, so
it can be read, tested and removed on its own: taking a feature out is
deleting its `include_router` line in main.py.
"""
