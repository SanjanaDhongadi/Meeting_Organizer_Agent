"""Deterministic room catalog for DEMO_MODE; employee profiles come from the database API."""
from typing import List, Dict, Any

SEED_ROOMS = [
    {
        "name": "Auditorium Alpha",
        "capacity": 150,
        "equipment": "Projector, Dual Wireless Mics, Livestream Rig, Stage Lighting",
        "location": "HQ Central Building, Ground Floor",
        "requires_approval": True
    },
    {
        "name": "Conference Room B (Innovation Lab)",
        "capacity": 14,
        "equipment": "4K Display, Polycom Video Bar, Whiteboard, HDMI Cables",
        "location": "HQ Building A, Floor 2",
        "requires_approval": False
    },
    {
        "name": "Executive Boardroom",
        "capacity": 20,
        "equipment": "Cisco Telepresence, Smart Digital Whiteboard, Catering Prep",
        "location": "HQ Executive Suite, Floor 5",
        "requires_approval": True
    }
]
