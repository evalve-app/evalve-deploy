"""The Supervisor of a Loop task.

`evalve build` copies the files of this package into each agent image, at
`/opt/evalve/`. The system Python of the image runs `supervisor.py`, so the
files import only the standard library.
"""
