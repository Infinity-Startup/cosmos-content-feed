"""Bounded Sydney-day scheduling, publication receipts, and honest update health."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo('Australia/Sydney')
MAX_DAILY_ATTEMPTS = 4
RETRY_INTERVAL_SECONDS = 2 * 60 * 60


def load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')


def gate(state, event, now):
    today = now.astimezone(SYDNEY).date().isoformat()
    if event != 'schedule':
        return True, 'Explicit update requested'
    if state.get('lastCompleteDate') == today:
        return False, 'Today has already been updated and published'
    if state.get('attemptDate') == today:
        if state.get('attempts', 0) >= MAX_DAILY_ATTEMPTS:
            return False, 'Daily retry budget reached; next attempt is tomorrow'
        last = datetime.fromisoformat(state['lastAttemptAt'])
        if (now - last).total_seconds() < RETRY_INTERVAL_SECONDS:
            return False, 'Waiting for the two-hour retry interval'
    return True, 'Daily update or bounded recovery attempt is due'


def record_attempt(state, now):
    today = now.astimezone(SYDNEY).date().isoformat()
    return {**state, 'attemptDate': today,
            'attempts': state.get('attempts', 0) + 1 if state.get('attemptDate') == today else 1,
            'lastAttemptAt': now.isoformat()}


def healthy(report):
    return report.get('status') == 'complete' and not report.get('portraitStageError') and not report.get('portraitSourceFailures', 0) and not report.get('portraitDeferred', 0)


def record_publication(state, report, now, targeted=False):
    state = {**state, 'lastPublishedAt': now.isoformat()}
    if not targeted and healthy(report):
        # Credit the day the source check started, not tomorrow if the job crossed midnight.
        checked = datetime.fromisoformat(report['checkedAt']).astimezone(SYDNEY).date().isoformat()
        state['lastCompleteDate'] = checked
    return state


def report_summary(report):
    lines = [f"Chapter checks: {report.get('titlesUpdated', 0)}/{report.get('titlesTotal', report.get('titlesChecked', 0))} updated; {report.get('titlesRetained', 0)} retained from the last valid snapshot.",
             f"New chapters: {len(report.get('newChapters', []))}. Portraits generated: {report.get('portraitsGenerated', 0)}. Portraits queued: {report.get('portraitsAwaitingReview', 0)}."]
    for item in report.get('titleFailures', [])[:12]:
        lines.append(f"- {item['titleId']}: {item['reason']}")
    if report.get('portraitStageError'):
        lines.append('Portrait stage: ' + report['portraitStageError'])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['gate', 'attempt', 'publication', 'summary', 'health', 'portrait-error', 'target'])
    parser.add_argument('--seed', type=Path, default=Path('seed'))
    parser.add_argument('--site', type=Path, default=Path('_site'))
    parser.add_argument('--event', default='workflow_dispatch')
    parser.add_argument('--targeted', action='store_true')
    parser.add_argument('--error', default='Portrait tools failed; chapter updates and existing artwork were preserved')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    state_path = args.seed / 'daily-state.json'
    state = load(state_path)
    if args.action == 'gate':
        run, reason = gate(state, args.event, now)
        print(reason)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('run=' + str(run).lower() + '\n')
    elif args.action == 'attempt':
        save(state_path, record_attempt(state, now))
    else:
        report_path = args.site / 'update-report.json'
        report = load(report_path)
        if args.action == 'target':
            for key in ('portraitStageError', 'portraitSourceFailures', 'portraitDeferred'):
                report.pop(key, None)
            report.update(status='targeted', sourceRequestsStopped=False, titlesChecked=0, titlesUpdated=0, titlesRetained=0, titleFailures=[], newChapters=[])
            save(report_path, report)
        elif args.action == 'publication':
            save(state_path, record_publication(state, report, now, args.targeted))
        elif args.action == 'portrait-error':
            report['portraitStageError'] = args.error
            save(report_path, report)
        elif args.action == 'summary':
            summary = report_summary(report)
            print(summary)
            if report.get('portraitsAwaitingReview'):
                print('::warning::Some episode portraits remain queued; see portrait-review-queue.json for reasons.')
            if not healthy(report) and not args.targeted and report.get('status') != 'targeted':
                print('::warning::Partial update: valid titles can publish, but failed source checks will be retried.')
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
                    output.write(summary)
        elif args.action == 'health' and not args.targeted and not healthy(report):
            raise SystemExit('Valid updates were published, but source/portrait failures remain; see the summary. Automatic recovery remains due.')


if __name__ == '__main__':
    main()
