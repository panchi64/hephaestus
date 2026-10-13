"""The dev instance — a second, disposable Hephaestus an agent can drive and look at.

The suite proves behaviour; this is for the claims a suite cannot make. A change to a
picker, a card, a layout or a flow is either seen working in the running product or it is
taken on trust, and the only running instance otherwise belongs to the operator, holding
their conversations and their credentials.

So: an instance with its own data directory, its own ports, its own platform VM and a
scripted model behind it, brought up by one command and seeded so every surface renders
populated. Nothing in here is imported by the app — it configures and launches the app
from outside, through the same environment any deployment would use.

Start at :mod:`devkit.instance` for what separates one instance from another, and at
:mod:`devkit.cli` for what a session actually runs.
"""
