"""Exact-count ACA-Py basic-message benchmark.

Establishes CONNECTIONS_PER_AGENT connections per Locust user during warmup,
waits until all users are ready, then sends exactly TARGET_MESSAGE_COUNT
end-to-end basic messages (issuer send + Credo receipt) with no inter-task wait.
"""

import os
import sys

import gevent
from locust import SequentialTaskSet, constant, events, task
from locust.exception import StopUser
from locustCustom import CustomLocust

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import basicmsg_counter as counter

WITH_MEDIATION = os.getenv("WITH_MEDIATION", "").strip().lower() in (
    "true",
    "1",
    "yes",
)
NUMBER_OF_CONNECTIONS = int(os.getenv("CONNECTIONS_PER_AGENT", "1"))
MESSAGE_TO_SEND = os.getenv("MESSAGE_TO_SEND", "ping")
MEASURE_MODE = os.getenv("MEASURE_MODE", "e2e").strip().lower()


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    counter.reset(
        target=os.getenv("TARGET_MESSAGE_COUNT", "10000"),
        ready_target=os.getenv("LOCUST_USERS", "1"),
    )
    print(
        f"[benchmark] target={counter.target()} users={os.getenv('LOCUST_USERS', '1')} "
        f"connections_per_agent={NUMBER_OF_CONNECTIONS} mediation={WITH_MEDIATION} "
        f"measure_mode={MEASURE_MODE} payload_bytes={len(MESSAGE_TO_SEND.encode('utf-8'))}",
        flush=True,
    )


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    def watch_and_quit():
        while not counter.is_done():
            gevent.sleep(0.2)
        # Let in-flight msg_client stopwatches finish and CSV flush.
        gevent.sleep(1.5)
        stats = counter.steady_state_stats()
        print(
            f"[benchmark] finished claimed={stats['count']} target={counter.target()} "
            f"steady_state_seconds={stats['seconds']:.3f} steady_state_rps={stats['rps']:.2f}",
            flush=True,
        )
        if environment.runner is not None:
            environment.runner.quit()

    gevent.spawn(watch_and_quit)


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    stats = counter.steady_state_stats()
    print(
        f"[benchmark] test_stop claimed={stats['count']} steady_state_rps={stats['rps']:.2f}",
        flush=True,
    )


class UserBehaviour(SequentialTaskSet):
    def on_start(self):
        self.client.startup(withMediation=WITH_MEDIATION)
        self.invites = []
        while len(self.invites) < NUMBER_OF_CONNECTIONS:
            self.client.ensure_is_running()
            invite = self.client.issuer_getinvite()
            connection = self.client.accept_invite(invite["invitation_url"])
            if connection is None:
                raise Exception("Failed to accept invitation")
            self.invites.append(invite)
        self.conn_idx = 0
        counter.mark_ready()

    def on_stop(self):
        self.client.shutdown()

    @task
    def msg_client(self):
        if not counter.claim_attempt():
            raise StopUser()

        invite = self.invites[self.conn_idx % len(self.invites)]
        self.conn_idx += 1
        try:
            if MEASURE_MODE in ("fastpath_admin", "fastpath-admin"):
                self.client.msg_client_fastpath_admin_only(invite["connection_id"])
            elif MEASURE_MODE in ("fastpath", "fastpath_e2e", "fastpath-e2e"):
                self.client.msg_client_fastpath(invite["connection_id"])
            elif MEASURE_MODE in ("admin", "admin_only", "admin-only"):
                self.client.msg_client_admin_only(invite["connection_id"])
            else:
                self.client.msg_client(invite["connection_id"])
        finally:
            counter.mark_send_complete()

        if counter.is_done():
            raise StopUser()


class BasicMsgBenchmark(CustomLocust):
    tasks = [UserBehaviour]
    wait_time = constant(0)
