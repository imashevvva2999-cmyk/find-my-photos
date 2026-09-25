"""Set the admin password. Only a scrypt hash is stored (ADMIN_PASSWORD_HASH in .env).

    .venv/bin/python scripts/set_admin_password.py              # asks for a new password
    .venv/bin/python scripts/set_admin_password.py --generate   # creates and prints a random one
    .venv/bin/python scripts/set_admin_password.py --convert    # hashes an old plain ADMIN_PASSWORD

Changing the password logs out every open admin session.
"""
import getpass
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.envfile import ENV_FILE, load_env_file, update_env_file  # noqa: E402
from app.passwords import hash_password  # noqa: E402

MIN_LENGTH = 12


def main() -> None:
    if "--convert" in sys.argv:
        load_env_file()
        plain = os.environ.get("ADMIN_PASSWORD")
        if not plain or os.environ.get("ADMIN_PASSWORD_HASH"):
            return
        update_env_file({"ADMIN_PASSWORD_HASH": hash_password(plain), "ADMIN_PASSWORD": None})
        print("  The admin password is now stored only as a hash (your password is unchanged).")
        return
    if "--generate" in sys.argv:
        password = secrets.token_urlsafe(12)
        print(f"\n  Your admin password is: {password}\n  (Only a hash is stored; write the password down now.)\n")
    else:
        password = getpass.getpass("New admin password (at least 12 characters): ")
        if len(password) < MIN_LENGTH:
            sys.exit(f"Too short: use at least {MIN_LENGTH} characters.")
        if getpass.getpass("Repeat it: ") != password:
            sys.exit("The two passwords are different.")
    update_env_file({"ADMIN_PASSWORD_HASH": hash_password(password), "ADMIN_PASSWORD": None})
    print(f"  Saved to {ENV_FILE.name}. Restart the site for the change to take effect.")


if __name__ == "__main__":
    main()
