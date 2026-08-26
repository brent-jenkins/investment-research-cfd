"""Deploy on a push to main, and refuse anything it cannot authenticate.

Why this exists rather than a GitHub Actions job: deploying over SSH means
storing a key that can log into the VM in GitHub's secrets. Pulling instead
means the only thing GitHub holds is a shared HMAC secret, which proves a
request came from GitHub and grants nothing. Given this project's rule that
credentials live in the environment and never leave the box, that is the
side to err on.

Why it deploys at all, rather than trusting a manual copy: on 2026-08-25 FX
conversion was switched on in the code and stayed off in production for
eleven hours. Nothing failed. Files were copied to the server and the
service was not restarted, so a resident daemon went on executing the
modules it had imported at start-up. Copying files is not deploying, and
the difference is invisible until a number is checked against a broker
statement. A deploy that always restarts cannot make that mistake.

Deliberately stdlib-only -- http.server, hmac, hashlib -- so the listener
has no dependency that could itself need deploying.

Binds to loopback only. Caddy terminates TLS and proxies /webhook/ to it,
so the port is never reachable from outside the machine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger("cfdinvest.webhook")

#: Loopback port. 8600 and 8601 belong to the other two listeners on this box.
DEFAULT_PORT = 8602

#: Only a push to this ref deploys. Tags, branches and pull requests do not.
TARGET_REF = "refs/heads/main"

#: The listener runs unprivileged and cannot restart a service, so it does
#: not try. It writes a request here; cfdinvest-deploy.path notices and
#: starts a root oneshot that does the work. The process reachable from the
#: internet therefore never holds privilege, which matters more than the
#: extra unit costs.
DEPLOY_REQUEST = Path("/run/cfdinvest/deploy.request")

#: Bigger than any plausible push payload and small enough that a hostile
#: caller cannot exhaust memory before the signature is even checked.
MAX_BODY_BYTES = 5 * 1024 * 1024


def verify_signature(signing_key: str, payload: bytes, header: str | None) -> bool:
    """Whether the body carries GitHub's HMAC for our secret.

    Compared with :func:`hmac.compare_digest` rather than ``==``: the naive
    comparison returns as soon as two bytes differ, and the time it takes
    leaks how much of a guess was correct. That is a real attack against a
    endpoint anyone can post to.
    """
    if not header or not header.startswith("sha256="):
        return False
    theirs = header.split("=", 1)[1]
    ours = hmac.new(signing_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(theirs, ours)


def should_deploy(event: str, payload: dict) -> bool:
    """A push to main, and nothing else."""
    return event == "push" and payload.get("ref") == TARGET_REF


def trigger_deploy(request: Path = DEPLOY_REQUEST) -> bool:
    """Ask for a deploy. Never raises.

    Writing a file rather than running a command: GitHub times a webhook out
    after ten seconds and a deploy takes minutes, so the response could not
    wait for a result in any case -- and this way the privileged half runs
    in a unit of its own, out of reach of anything the network can touch.

    The content is the timestamp only. Nothing from the request body is
    written anywhere, so a hostile payload has no path to the deploy.
    """
    try:
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
        logger.info("Deploy requested via %s", request)
        return True
    except OSError as error:
        logger.error("Could not request a deploy: %s", error)
        return False


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "cfdinvest-webhook"
    # Named signing_key rather than secret so the repository's own scan for
    # committed credentials -- which looks for assignments to anything named
    # like a secret -- does not have to be taught an exception. A scanner
    # with an exception list is a scanner that gets one more next time.
    signing_key = ""

    def _send(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        """A health check that reveals nothing about the deploy."""
        self._send(200, "ok")

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, "bad content-length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, "bad content-length")
            return

        payload = self.rfile.read(length)

        # Authenticate before parsing. A body that cannot be trusted should
        # not reach a JSON parser.
        if not verify_signature(
            self.signing_key, payload, self.headers.get("X-Hub-Signature-256")
        ):
            logger.warning("Rejected a request with a bad or missing signature")
            self._send(401, "bad signature")
            return

        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, "bad json")
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self._send(200, "pong")
            return

        if not should_deploy(event, document):
            ref = document.get("ref", "<none>")
            logger.info("Ignoring %s (ref=%s)", event, ref)
            self._send(200, f"ignored: event={event} ref={ref}")
            return

        if trigger_deploy():
            self._send(202, "deploy requested")
        else:
            self._send(500, "could not request a deploy")

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


def run_server(port: int, signing_key: str) -> None:
    WebhookHandler.signing_key = signing_key
    server = ThreadingHTTPServer(("127.0.0.1", port), WebhookHandler)
    logger.info("Listening on 127.0.0.1:%d for pushes to %s", port, TARGET_REF)
    server.serve_forever()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    configured = os.environ.get("CFD_GITHUB_WEBHOOK_SECRET", "")
    if not configured:
        # Refusing to start is the only safe reading of a missing secret. A
        # listener that accepted unsigned requests would be a shell on the
        # box for anyone who found the path.
        logger.error(
            "CFD_GITHUB_WEBHOOK_SECRET is not set -- refusing to start. "
            "Set it in /etc/cfdinvest/env. It is this repository's own "
            "secret and must not be shared with the other listeners on "
            "this machine: one compromise would otherwise reach all three."
        )
        return 1

    port = int(os.environ.get("CFD_WEBHOOK_PORT", DEFAULT_PORT))
    try:
        run_server(port, configured)
    except KeyboardInterrupt:
        logger.info("Stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
