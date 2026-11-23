"""Alert channels. The default one writes a JSON line to the console."""

import json

from .monitors import Monitor


def log_alert(monitor: Monitor) -> None:
    """Print the alert as a single JSON line.

    This is where an email or webhook call would go in a real deployment.
    """
    print(
        json.dumps(
            {
                "ALERT": f"Device {monitor.id} is down!",
                "time": monitor.last_alert_at.isoformat() if monitor.last_alert_at else None,
                "alert_email": monitor.alert_email,
            }
        ),
        flush=True,
    )
