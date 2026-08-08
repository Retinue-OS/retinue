import sys
import os
import json
import tempfile
import threading
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath("scripts"))
import push_notify

def test_subscribe_merges_mode():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp).absolute()
        push_notify.init(tmp_path)
        
        subscription = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "abc", "auth": "123"}
        }
        payload = {
            "subscription": subscription,
            "notification_mode": "new_only"
        }
        
        success = push_notify.subscribe(payload)
        assert success is True
        
        digest = hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
        subs_dir = tmp_path / "subscriptions"
        record_file = subs_dir / f"{digest}.json"
        
        assert record_file.exists()
        with open(record_file, "r") as f:
            data = json.load(f)
            assert data["notification_mode"] == "new_only"
            assert data["keys"]["p256dh"] == "abc"

def test_notify_filters_by_mode():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp).absolute()
        push_notify.init(tmp_path)
        
        sub_endpoint = "https://example.com/stalled_sub"
        sub_data = {
            "endpoint": sub_endpoint,
            "keys": {"p256dh": "abc", "auth": "123"},
            "notification_mode": "stalled_only"
        }
        
        digest = hashlib.sha256(sub_endpoint.encode()).hexdigest()
        subs_dir = tmp_path / "subscriptions"
        record_file = subs_dir / f"{digest}.json"
        record_file.parent.mkdir(parents=True, exist_ok=True)
        with open(record_file, "w") as f:
            json.dump(sub_data, f)
        
        with patch("push_notify.webpush") as mock_webpush:
            # Case A: User is 'stalled_only'. If event is 'new', it should be skipped.
            push_notify.notify(title="Test", body="Body", url="/", tag="tag", mode="new")
            mock_webpush.assert_not_called()
            
            # Case B: User is 'stalled_only'. If event is 'stalled', it should be sent.
            push_notify.notify(title="Test", body="Body", url="/", tag="tag", mode="stalled")
            assert mock_webpush.call_count == 1

if __name__ == "__main__":
    print("Running tests...")
    try:
        test_subscribe_merges_mode()
        print("✓ test_subscribe_merges_mode passed")
        test_notify_filters_by_mode()
        print("✓ test_notify_filters_by_mode passed")
        print("All tests passed!")
    except Exception as e:
        print(f"Tests failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
