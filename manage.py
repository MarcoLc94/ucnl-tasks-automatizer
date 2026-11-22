"""
Administración de usuarios del bot.

    python manage.py add-user marco --name "Marco Antonio Lopez Castro" --import-legacy
    python manage.py list-users
    python manage.py set-password marco
    python manage.py delete-user juan
"""
import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from bot import db, state, users  # noqa: E402
from bot.security import check_secret_key  # noqa: E402

_ROOT = Path(__file__).parent


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Contraseña del panel (mín. 8 caracteres): ")
        if len(pw) < 8:
            print("Muy corta.")
            continue
        if pw != getpass.getpass("Repite la contraseña: "):
            print("No coinciden.")
            continue
        return pw


def add_user(args):
    if users.get_by_username(args.username):
        sys.exit(f"El usuario '{args.username}' ya existe")
    name = args.name or input("Nombre completo (para las portadas): ").strip()
    user = users.create_user(args.username, _ask_password(), name, args.hour, args.minute)
    print(f"Usuario creado: {user.username} (id {user.id}) — escaneo diario {user.scan_hour:02d}:{user.scan_minute:02d}")

    if args.import_legacy:
        _import_legacy(user)
    else:
        print("El usuario debe entrar al panel → 'Mi cuenta' para poner su usuario y contraseña de UCNL.")
    print("Reinicia el servidor para que se programe su escaneo diario.")


def _import_legacy(user: users.User):
    """Migra los datos de la versión de un solo usuario (.env, tasks.json, .auth_state.json)."""
    legacy_tasks = db.DATA_DIR / "tasks.json"
    if legacy_tasks.exists():
        state.import_legacy_json(legacy_tasks, user.id)
        legacy_tasks.rename(legacy_tasks.with_suffix(".json.migrated"))
        print(f"Tareas importadas de {legacy_tasks.name} (renombrado a tasks.json.migrated)")

    ucnl_user, ucnl_pass = os.getenv("UCNL_USERNAME", ""), os.getenv("UCNL_PASSWORD", "")
    if ucnl_user and ucnl_pass:
        users.update_profile(user.id, ucnl_username=ucnl_user, ucnl_password=ucnl_pass)
        print("Credenciales de UCNL importadas de .env (cifradas). Ya puedes borrarlas del .env.")

    legacy_auth = _ROOT / ".auth_state.json"
    if legacy_auth.exists():
        shutil.move(str(legacy_auth), users.get_user(user.id).auth_state_path)
        print("Sesión de UCNL guardada movida a la carpeta del usuario")


def list_users(_args):
    rows = users.list_users()
    if not rows:
        print("No hay usuarios.")
    for u in rows:
        creds = "sí" if u.has_ucnl_credentials else "NO"
        print(f"{u.id:>3}  {u.username:<15} {u.display_name:<35} escaneo {u.scan_hour:02d}:{u.scan_minute:02d}  UCNL: {creds}")


def set_password(args):
    user = users.get_by_username(args.username)
    if not user:
        sys.exit(f"No existe el usuario '{args.username}'")
    users.set_password(user.id, _ask_password())
    print("Contraseña actualizada (se cerraron sus sesiones abiertas).")


def delete_user(args):
    user = users.get_by_username(args.username)
    if not user:
        sys.exit(f"No existe el usuario '{args.username}'")
    if input(f"¿Borrar a '{user.username}' con todas sus tareas? Escribe el usuario para confirmar: ") != user.username:
        sys.exit("Cancelado")
    data_dir = user.data_dir
    users.delete_user(user.id)
    shutil.rmtree(data_dir, ignore_errors=True)
    print("Usuario eliminado.")


def main():
    parser = argparse.ArgumentParser(description="Administración de usuarios del UCNL Task Bot")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("add-user", help="Crear un usuario")
    p.add_argument("username")
    p.add_argument("--name", help="Nombre completo para las portadas")
    p.add_argument("--hour", type=int, default=8, help="Hora del escaneo diario (0-23)")
    p.add_argument("--minute", type=int, default=0)
    p.add_argument("--import-legacy", action="store_true",
                   help="Importar tasks.json, .auth_state.json y credenciales UCNL del .env a este usuario")
    p.set_defaults(func=add_user)

    sub.add_parser("list-users", help="Listar usuarios").set_defaults(func=list_users)

    p = sub.add_parser("set-password", help="Cambiar la contraseña del panel de un usuario")
    p.add_argument("username")
    p.set_defaults(func=set_password)

    p = sub.add_parser("delete-user", help="Eliminar un usuario y sus tareas")
    p.add_argument("username")
    p.set_defaults(func=delete_user)

    args = parser.parse_args()
    check_secret_key()
    args.func(args)


if __name__ == "__main__":
    main()
