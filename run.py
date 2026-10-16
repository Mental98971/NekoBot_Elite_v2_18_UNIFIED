import time
import subprocess
import sys

def main():
    max_retries = 10
    retry_delay = 5
    
    for attempt in range(1, max_retries + 1):
        print(f"[Launcher] Starting Neko Bot (Attempt {attempt}/{max_retries})...")
        process = subprocess.Popen([sys.executable, "-m", "bot"])
        
        # Wait for process to finish or crash
        exit_code = process.wait()
        
        if exit_code == 0:
            print("[Launcher] Bot exited cleanly.")
            break
        else:
            print(f"[Launcher] Bot crashed with exit code {exit_code}. Restarting in {retry_delay} seconds...")
            time.sleep(retry_delay)

if __name__ == "__main__":
    main()
