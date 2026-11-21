"""Session 16: only one engine (monitor + workers) may run per data folder."""
import subprocess
import sys
import textwrap

from clipengine import __main__ as m


def test_second_engine_on_same_data_folder_is_refused(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time; sys.path.insert(0, {str(m.PROJECT_ROOT)!r})
        from clipengine.__main__ import acquire_instance_lock
        assert acquire_instance_lock({str(tmp_path)!r}); print("held", flush=True); time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert m.acquire_instance_lock(tmp_path) is False
    finally:
        holder.kill()
        holder.wait()
    # the OS releases the lock when the holder dies (even when killed), so a restart works
    assert m.acquire_instance_lock(tmp_path) is True
    m._INSTANCE_LOCK.close()
    m._INSTANCE_LOCK = None
