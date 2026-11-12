"""Only one bot per token: a second one refuses to start."""
import subprocess
import sys
import textwrap


def test_a_second_bot_refuses_to_start(tmp_path):
    lock = tmp_path / "bot.lock"
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, "src")
        from ainl.bot.__main__ import single_instance
        ok = single_instance({str(lock)!r})
        print("OK" if ok else "NO", flush=True)
        if ok:
            time.sleep(3)
    """)
    first = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert first.stdout.readline().strip() == "OK"
    second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert second.stdout.strip() == "NO" and f"pid {first.pid}" in second.stderr
    first.wait(timeout=10)
    third = subprocess.run([sys.executable, "-c", code.replace("time.sleep(3)", "pass")],
                           capture_output=True, text=True, timeout=10)
    assert third.stdout.strip() == "OK"                   # the lock left with the first process
