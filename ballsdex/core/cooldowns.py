import json
from datetime import datetime, timedelta, timezone

COOLDOWN_FILE = "cooldowns.json"

def _load_cooldowns():
    """Loads cooldown data from the JSON file."""
    try:
        with open(COOLDOWN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_cooldowns(data):
    """Saves cooldown data to the JSON file."""
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_cooldown(user_id: int, key: str) -> datetime | None:
    """Gets the last used time for a key (daily/weekly) for a user."""
    data = _load_cooldowns()
    user_id_str = str(user_id)
    
    if user_id_str in data and key in data[user_id_str]:
        # Convert stored ISO string back to datetime object
        return datetime.fromisoformat(data[user_id_str][key])
        
    return None

def set_cooldown(user_id: int, key: str):
    """Sets the current time (UTC) as the last used time for a key."""
    data = _load_cooldowns()
    user_id_str = str(user_id)
    
    if user_id_str not in data:
        data[user_id_str] = {}
    
    # Store time in ISO format (timezone-aware)
    data[user_id_str][key] = datetime.now(timezone.utc).isoformat()
    _save_cooldowns(data)