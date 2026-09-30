"""HTTP API for local tweet runs. No model calls."""

from flask import jsonify, request

from ..tweet.service import (
    TweetRunError,
    cancel_run,
    create_run,
    delete_run,
    get_run,
    list_runs,
    report_view,
)
from . import tweet_bp


def _error(exc: TweetRunError):
    return jsonify(success=False, error=exc.message, error_code=exc.error_code), exc.status_code


@tweet_bp.route("/runs", methods=["GET"])
def list_tweet_runs():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(success=True, runs=list_runs(limit or 50))


@tweet_bp.route("/runs", methods=["POST"])
def create_tweet_run():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    try:
        run, _created = create_run(payload, idempotency_key=key or "")
    except TweetRunError as exc:
        return _error(exc)
    return jsonify(success=True, run_id=run["run_id"], status=run["status"], run=run), 202


@tweet_bp.route("/runs/<run_id>", methods=["GET"])
def get_tweet_run(run_id: str):
    try:
        run = get_run(run_id)
    except TweetRunError as exc:
        return _error(exc)
    return jsonify(success=True, run=run)


@tweet_bp.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_tweet_run(run_id: str):
    try:
        run = cancel_run(run_id)
    except TweetRunError as exc:
        return _error(exc)
    return jsonify(success=True, run=run)


@tweet_bp.route("/runs/<run_id>/report", methods=["GET"])
def get_tweet_report(run_id: str):
    try:
        body = report_view(run_id)
    except TweetRunError as exc:
        return _error(exc)
    return jsonify(success=True, **body)


@tweet_bp.route("/runs/<run_id>", methods=["DELETE"])
def delete_tweet_run(run_id: str):
    try:
        delete_run(run_id)
    except TweetRunError as exc:
        return _error(exc)
    return jsonify(success=True, deleted=True)
