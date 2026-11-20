"""
Room / auditorium catalog used by the Resource Agent and the request parser.
Organizations supply their own catalog with ROOM_CATALOG_FILE=/path/to/rooms.json (a JSON list of objects with
name, capacity, equipment, location, requires_approval). The list below is only the default.
Employee profiles are never seeded; they come from the database API.
"""
import json
import os
from typing import List, Dict, Any

DEFAULT_ROOMS = [
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


def load_room_catalog() -> List[Dict[str, Any]]:
    path = os.getenv("ROOM_CATALOG_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return DEFAULT_ROOMS


SEED_ROOMS = load_room_catalog()
