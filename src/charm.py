#!/usr/bin/env python3
"""Charm code for MongoDB service on Kubernetes."""
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
import time
from typing import Dict, List, Optional, Set

from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from charms.mongodb.v0.helpers import (
    build_unit_status,
    generate_keyfile,
    generate_password,
    get_create_user_cmd,
    get_mongod_args,
    process_pbm_error,
)
from charms.mongodb.v0.mongodb import (
    MongoDBConfiguration,
    MongoDBConnection,
    NotReadyError,
)
from charms.mongodb.v0.mongodb_backups import S3_RELATION, MongoDBBackups
from charms.mongodb.v0.mongodb_provider import MongoDBProvider
from charms.mongodb.v0.mongodb_tls import MongoDBTLS
from charms.mongodb.v0.users import (
    CHARM_USERS,
    BackupUser,
    MongoDBUser,
    MonitorUser,
    OperatorUser,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops import JujuVersion
from ops.charm import (
    ActionEvent,
    CharmBase,
    RelationDepartedEvent,
    StartEvent,
    UpdateStatusEvent,
)
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    Container,
    Relation,
    RelationDataContent,
    SecretNotFoundError,
    Unit,
    WaitingStatus,
)
from ops.pebble import (
    ChangeError,
    ExecError,
    Layer,
    PathError,
    ProtocolError,
    ServiceInfo,
)
from pymongo.errors import PyMongoError
from tenacity import before_log, retry, stop_after_attempt, wait_fixed

from config import Config
from exceptions import AdminUserCreationError, MissingSecretError, SecretNotAddedError

logger = logging.getLogger(__name__)

UNIT_REMOVAL_TIMEOUT = 1000

APP_SCOPE = Config.Relations.APP_SCOPE
UNIT_SCOPE = Config.Relations.UNIT_SCOPE
Scopes = Config.Relations.Scopes


class MongoDBCharm(CharmBase):
    """A Juju Charm to deploy MongoDB on Kubernetes."""

    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.mongod_pebble_ready, self._on_mongod_pebble_ready)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(
            self.on[Config.Relations.PEERS].relation_joined, self._relation_changes_handler
        )

        self.framework.observe(
            self.on[Config.Relations.PEERS].relation_changed, self._relation_changes_handler
        )

        self.framework.observe(
            self.on[Config.Relations.PEERS].relation_departed, self._relation_changes_handler
        )

        # if a new leader has been elected update hosts of MongoDB
        self.framework.observe(self.on.leader_elected, self._relation_changes_handler)

        self.framework.observe(self.on.get_password_action, self._on_get_password)
        self.framework.observe(self.on.set_password_action, self._on_set_password)
        self.framework.observe(self.on.stop, self._on_stop)

        self.framework.observe(self.on.secret_remove, self._on_secret_remove)
        self.framework.observe(self.on.secret_changed, self._on_secret_changed)

        self.client_relations = MongoDBProvider(self)
        self.tls = MongoDBTLS(self, Config.Relations.PEERS, Config.SUBSTRATE)
        self.backups = MongoDBBackups(self, substrate=Config.SUBSTRATE)

        self.metrics_endpoint = MetricsEndpointProvider(
            self, refresh_event=self.on.start, jobs=Config.Monitoring.JOBS
        )
        self.grafana_dashboards = GrafanaDashboardProvider(self)
        self.loki_push = LogProxyConsumer(
            self,
            log_files=Config.LOG_FILES,
            relation_name=Config.Relations.LOGGING,
            container_name=Config.CONTAINER_NAME,
        )
        self.secrets = {APP_SCOPE: {}, UNIT_SCOPE: {}}

    # BEGIN: properties

    @property
    def _unit_hosts(self) -> List[str]:
        """Retrieve IP addresses associated with MongoDB application.

        Returns:
            a list of IP address associated with MongoDB application.
        """
        self_unit = [self.get_hostname_for_unit(self.unit)]

        if not self._peers:
            return self_unit

        return self_unit + [self.get_hostname_for_unit(unit) for unit in self._peers.units]

    @property
    def _peers(self) -> Optional[Relation]:
        """Fetch the peer relation.

        Returns:
             An `ops.model.Relation` object representing the peer relation.
        """
        return self.model.get_relation(Config.Relations.PEERS)

    @property
    def mongodb_config(self) -> MongoDBConfiguration:
        """Create a configuration object with settings.

        Needed for correct handling interactions with MongoDB.

        Returns:
            A MongoDBConfiguration object
        """
        return self._get_mongodb_config_for_user(OperatorUser, self._unit_hosts)

    @property
    def monitor_config(self) -> MongoDBConfiguration:
        """Generates a MongoDBConfiguration object for this deployment of MongoDB."""
        return self._get_mongodb_config_for_user(
            MonitorUser, [self.get_hostname_for_unit(self.unit)]
        )

    @property
    def backup_config(self) -> MongoDBConfiguration:
        """Generates a MongoDBConfiguration object for backup."""
        return self._get_mongodb_config_for_user(
            BackupUser, [self.get_hostname_for_unit(self.unit)]
        )

    @property
    def _mongod_layer(self) -> Layer:
        """Returns a Pebble configuration layer for mongod."""
        layer_config = {
            "summary": "mongod layer",
            "description": "Pebble config layer for replicated mongod",
            "services": {
                "mongod": {
                    "override": "replace",
                    "summary": "mongod",
                    "command": "mongod " + get_mongod_args(self.mongodb_config),
                    "startup": "enabled",
                    "user": Config.UNIX_USER,
                    "group": Config.UNIX_GROUP,
                }
            },
        }
        return Layer(layer_config)  # type: ignore

    @property
    def _monitor_layer(self) -> Layer:
        """Returns a Pebble configuration layer for mongodb_exporter."""
        layer_config = {
            "summary": "mongodb_exporter layer",
            "description": "Pebble config layer for mongodb_exporter",
            "services": {
                "mongodb_exporter": {
                    "override": "replace",
                    "summary": "mongodb_exporter",
                    "command": "mongodb_exporter --collector.diagnosticdata --compatible-mode",
                    "startup": "enabled",
                    "user": Config.UNIX_USER,
                    "group": Config.UNIX_GROUP,
                    "environment": {"MONGODB_URI": self.monitor_config.uri},
                }
            },
        }
        return Layer(layer_config)  # type: ignore

    @property
    def _backup_layer(self) -> Layer:
        """Returns a Pebble configuration layer for pbm."""
        layer_config = {
            "summary": "pbm layer",
            "description": "Pebble config layer for pbm",
            "services": {
                Config.Backup.SERVICE_NAME: {
                    "override": "replace",
                    "summary": "pbm",
                    "command": "pbm-agent",
                    "startup": "enabled",
                    "user": Config.UNIX_USER,
                    "group": Config.UNIX_GROUP,
                    "environment": {"PBM_MONGODB_URI": self.backup_config.uri},
                }
            },
        }
        return Layer(layer_config)  # type: ignore

    @property
    def relation(self) -> Optional[Relation]:
        """Peer relation data object."""
        return self.model.get_relation(Config.Relations.PEERS)

    @property
    def unit_peer_data(self) -> Dict:
        """Peer relation data object."""
        relation = self.relation
        if relation is None:
            return {}

        return relation.data[self.unit]

    @property
    def app_peer_data(self) -> RelationDataContent:
        """Peer relation data object."""
        relation = self.relation
        if relation is None:
            return {}

        return relation.data[self.app]

    @property
    def db_initialised(self) -> bool:
        """Check if MongoDB is initialised."""
        return "db_initialised" in self.app_peer_data

    @db_initialised.setter
    def db_initialised(self, value):
        """Set the db_initialised flag."""
        if isinstance(value, bool):
            self.app_peer_data["db_initialised"] = str(value)
        else:
            raise ValueError(
                f"'db_initialised' must be a boolean value. Proivded: {value} is of type {type(value)}"
            )

    # END: properties

    # BEGIN: generic helper methods

    def _scope_opj(self, scope: Scopes):
        if scope == APP_SCOPE:
            return self.app
        if scope == UNIT_SCOPE:
            return self.unit

    def _peer_data(self, scope: Scopes):
        return self.relation.data[self._scope_opj(scope)]

    @staticmethod
    def _compare_secret_ids(secret_id1: str, secret_id2: str) -> bool:
        """Reliable comparison on secret equality.

        NOTE: Secret IDs may be of any of these forms:
         - secret://9663a790-7828-4186-8b21-2624c58b6cfe/citb87nubg2s766pab40
         - secret:citb87nubg2s766pab40
        """
        if not secret_id1 or not secret_id2:
            return False

        regex = re.compile(".*[^/][/:]")

        pure_id1 = regex.sub("", secret_id1)
        pure_id2 = regex.sub("", secret_id2)

        if pure_id1 and pure_id2:
            return pure_id1 == pure_id2
        return False

    # END: generic helper methods

    # BEGIN: charm events
    def _on_mongod_pebble_ready(self, event) -> None:
        """Configure MongoDB pebble layer specification."""
        # Get a reference the container attribute
        container = self.unit.get_container(Config.CONTAINER_NAME)
        if not container.can_connect():
            logger.debug("mongod container is not ready yet.")
            event.defer()
            return

        try:
            # mongod needs keyFile and TLS certificates on filesystem
            self.push_tls_certificate_to_workload()
            self._push_keyfile_to_workload(container)
            self._pull_licenses(container)
            self._set_data_dir_permissions(container)

        except (PathError, ProtocolError, MissingSecretError) as e:
            logger.error("Cannot initialize workload: %r", e)
            event.defer()
            return

        # Add initial Pebble config layer using the Pebble API
        container.add_layer("mongod", self._mongod_layer, combine=True)
        # Restart changed services and start startup-enabled services.
        container.replan()

        # when a network cuts and the pod restarts - reconnect to the exporter
        try:
            self._connect_mongodb_exporter()
            self._connect_pbm_agent()
        except MissingSecretError as e:
            logger.error("Cannot connect mongodb exporter: %r", e)
            event.defer()
            return

    def _on_start(self, event) -> None:
        """Initialise MongoDB.

        Initialisation of replSet should be made once after start.
        MongoDB needs some time to become fully started.
        This event handler is deferred if initialisation of MongoDB
        replica set fails.
        By doing so, it is guaranteed that another
        attempt at initialisation will be made.

        Initial operator user can be created only through localhost connection.
        see https://www.mongodb.com/docs/manual/core/localhost-exception/
        unfortunately, pymongo unable to create a connection that is considered
        as local connection by MongoDB, even if a socket connection is used.
        As a result, there are only hackish ways to create initial user.
        It is needed to install mongodb-clients inside the charm container
        to make this function work correctly.
        """
        container = self.unit.get_container(Config.CONTAINER_NAME)
        if not container.can_connect():
            logger.debug("mongod container is not ready yet.")
            event.defer()
            return

        if not container.exists(Config.SOCKET_PATH):
            logger.debug("The mongod socket is not ready yet.")
            event.defer()
            return

        with MongoDBConnection(self.mongodb_config, "localhost", direct=True) as direct_mongo:
            if not direct_mongo.is_ready:
                logger.debug("mongodb service is not ready yet.")
                event.defer()
                return

        try:
            self._connect_mongodb_exporter()
        except ChangeError as e:
            logger.error(
                "An exception occurred when starting mongodb exporter, error: %s.", str(e)
            )
            self.unit.status = BlockedStatus("couldn't start mongodb exporter")
            return

        self._initialise_replica_set(event)

        # mongod is now active
        self.unit.status = ActiveStatus()

    def _relation_changes_handler(self, event) -> None:
        """Handles different relation events and updates MongoDB replica set."""
        self._connect_mongodb_exporter()
        self._connect_pbm_agent()

        if type(event) is RelationDepartedEvent:
            if event.departing_unit.name == self.unit.name:
                self.unit_peer_data.setdefault("unit_departed", "True")

        if not self.unit.is_leader():
            return

        # Admin password and keyFile should be created before running MongoDB.
        # This code runs on leader_elected event before mongod_pebble_ready
        self._generate_secrets()

        if not self.db_initialised:
            return

        with MongoDBConnection(self.mongodb_config) as mongo:
            try:
                replset_members = mongo.get_replset_members()
                mongodb_hosts = self.mongodb_config.hosts

                # compare sets of mongod replica set members and juju hosts
                # to avoid unnecessary reconfiguration.
                if replset_members == mongodb_hosts:
                    self._set_leader_unit_active_if_needed()
                    return

                logger.info("Reconfigure replica set")

                # remove members first, it is faster
                self._remove_units_from_replica_set(event, mongo, replset_members - mongodb_hosts)

                # to avoid potential race conditions -
                # remove unit before adding new replica set members
                if type(event) == RelationDepartedEvent and event.unit:
                    mongodb_hosts = mongodb_hosts - set([self.get_hostname_for_unit(event.unit)])

                self._add_units_from_replica_set(event, mongo, mongodb_hosts - replset_members)

                # app relations should be made aware of the new set of hosts
                self._update_app_relation_data(mongo.get_users())

            except NotReadyError:
                logger.info("Deferring reconfigure: another member doing sync right now")
                event.defer()
            except PyMongoError as e:
                logger.info("Deferring reconfigure: error=%r", e)
                event.defer()

    def _on_stop(self, event) -> None:
        if "True" == self.unit_peer_data.get("unit_departed", "False"):
            logger.debug(f"{self.unit.name} blocking on_stop")
            is_in_replica_set = True
            timeout = UNIT_REMOVAL_TIMEOUT
            while is_in_replica_set and timeout > 0:
                is_in_replica_set = self.is_unit_in_replica_set()
                time.sleep(1)
                timeout -= 1
                if timeout < 0:
                    raise Exception(f"{self.unit.name}.on_stop timeout exceeded")
            logger.debug(f"{self.unit.name} releasing on_stop")
            self.unit_peer_data["unit_departed"] = ""

    def _on_update_status(self, event: UpdateStatusEvent):
        # no need to report on replica set status until initialised
        if not self.db_initialised:
            return

        # Cannot check more advanced MongoDB statuses if mongod hasn't started.
        with MongoDBConnection(self.mongodb_config, "localhost", direct=True) as direct_mongo:
            if not direct_mongo.is_ready:
                self.unit.status = WaitingStatus("Waiting for MongoDB to start")
                return

        # leader should periodically handle configuring the replica set. Incidents such as network
        # cuts can lead to new IP addresses and therefore will require a reconfigure. Especially
        # in the case that the leader a change in IP address it will not receive a relation event.
        if self.unit.is_leader():
            self._relation_changes_handler(event)

        # update the units status based on it's replica set config and backup status. An error in
        # the status of MongoDB takes precedence over pbm status.
        mongodb_status = build_unit_status(
            self.mongodb_config, self.get_hostname_for_unit(self.unit)
        )
        pbm_status = self.backups._get_pbm_status()
        if (
            not isinstance(mongodb_status, ActiveStatus)
            or not self.model.get_relation(
                S3_RELATION
            )  # if s3 relation doesn't exist only report MongoDB status
            or isinstance(pbm_status, ActiveStatus)  # pbm is ready then report the MongoDB status
        ):
            self.unit.status = mongodb_status
        else:
            self.unit.status = pbm_status

    # END: charm events

    # BEGIN: actions
    def _on_get_password(self, event: ActionEvent) -> None:
        """Returns the password for the user as an action response."""
        username = self._get_user_or_fail_event(
            event, default_username=OperatorUser.get_username()
        )
        if not username:
            return
        key_name = MongoDBUser.get_password_key_name_for_user(username)
        event.set_results(
            {Config.Actions.PASSWORD_PARAM_NAME: self.get_secret(APP_SCOPE, key_name)}
        )

    def _on_set_password(self, event: ActionEvent) -> None:
        """Set the password for the specified user."""
        # only leader can write the new password into peer relation.
        if not self.unit.is_leader():
            event.fail("The action can be run only on leader unit.")
            return

        username = self._get_user_or_fail_event(
            event, default_username=OperatorUser.get_username()
        )
        if not username:
            return

        new_password = event.params.get(Config.Actions.PASSWORD_PARAM_NAME, generate_password())

        if new_password == self.get_secret(
            APP_SCOPE, MonitorUser.get_password_key_name_for_user(username)
        ):
            event.log("The old and new passwords are equal.")
            event.set_results({Config.Actions.PASSWORD_PARAM_NAME: new_password})
            return

        with MongoDBConnection(self.mongodb_config) as mongo:
            try:
                mongo.set_user_password(username, new_password)
            except NotReadyError:
                event.fail(
                    "Failed to change the password: Not all members healthy or finished initial sync."
                )
                return
            except PyMongoError as e:
                event.fail(f"Failed changing the password: {e}")
                return

        secret_id = self.set_secret(
            APP_SCOPE, MongoDBUser.get_password_key_name_for_user(username), new_password
        )

        if username == BackupUser.get_username():
            self._connect_pbm_agent()

        if username == MonitorUser.get_username():
            self._connect_mongodb_exporter()

        event.set_results(
            {Config.Actions.PASSWORD_PARAM_NAME: new_password, "secret-id": secret_id}
        )

    def _on_secret_remove(self, event):
        logging.debug(f"Secret {event.secret.id} seems to have no observers, could be removed")

    def _on_secret_changed(self, event):
        """Handles secrets changes event.

        When user run set-password action, juju leader changes the password inside the database
        and inside the secret object. This action runs the restart for monitoring tool and
        for backup tool on non-leader units to keep them working with MongoDB. The same workflow
        occurs on TLS certs change.
        """
        if self._compare_secret_ids(
            event.secret.id, self.app_peer_data.get(Config.Secrets.SECRET_INTERNAL_LABEL)
        ):
            scope = APP_SCOPE
        elif self._compare_secret_ids(
            event.secret.id, self.unit_peer_data.get(Config.Secrets.SECRET_INTERNAL_LABEL)
        ):
            scope = UNIT_SCOPE
        else:
            logging.debug("Secret %s changed, but it's unknown", event.secret.id)
            return
        logging.debug("Secret %s for scope %s changed, refreshing", event.secret.id, scope)

        self._juju_secrets_get(scope)
        self._connect_mongodb_exporter()
        self._connect_pbm_agent()

    # END: actions

    # BEGIN: user management
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        reraise=True,
        before=before_log(logger, logging.DEBUG),
    )
    def _init_operator_user(self) -> None:
        """Creates initial operator user for MongoDB.

        Initial operator user can be created only through localhost connection.
        see https://www.mongodb.com/docs/manual/core/localhost-exception/
        unfortunately, pymongo unable to create a connection that is considered
        as local connection by MongoDB, even if a socket connection is used.
        As a result, there are only hackish ways to create initial user.
        It is needed to install mongodb-clients inside the charm container
        to make this function work correctly.
        """
        if self._is_user_created(OperatorUser):
            return

        container = self.unit.get_container(Config.CONTAINER_NAME)

        mongo_cmd = (
            "/usr/bin/mongosh" if container.exists("/usr/bin/mongosh") else "/usr/bin/mongo"
        )

        process = container.exec(
            command=get_create_user_cmd(self.mongodb_config, mongo_cmd),
            stdin=self.mongodb_config.password,
        )
        try:
            process.wait_output()
        except Exception as e:
            logger.exception("Failed to create the operator user: %s", e)
            raise AdminUserCreationError

        logger.debug(f"{OperatorUser.get_username()} user created")
        self._set_user_created(OperatorUser)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        reraise=True,
        before=before_log(logger, logging.DEBUG),
    )
    def _init_monitor_user(self):
        """Creates the monitor user on the MongoDB database."""
        if self._is_user_created(MonitorUser):
            return

        with MongoDBConnection(self.mongodb_config) as mongo:
            logger.debug("creating the monitor user roles...")
            mongo.create_role(
                role_name=MonitorUser.get_mongodb_role(), privileges=MonitorUser.get_privileges()
            )
            logger.debug("creating the monitor user...")
            mongo.create_user(self.monitor_config)
            self._set_user_created(MonitorUser)

        self._connect_mongodb_exporter()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        reraise=True,
        before=before_log(logger, logging.DEBUG),
    )
    def _init_backup_user(self):
        """Creates the backup user on the MongoDB database."""
        if self._is_user_created(BackupUser):
            return

        with MongoDBConnection(self.mongodb_config) as mongo:
            # first we must create the necessary roles for the PBM tool
            logger.info("creating the backup user roles...")
            mongo.create_role(
                role_name=BackupUser.get_mongodb_role(), privileges=BackupUser.get_privileges()
            )

            self._check_or_set_user_password(BackupUser)
            mongo.create_user(self.backup_config)
            self._set_user_created(BackupUser)

    # END: user management

    # BEGIN: helper functions

    def _is_user_created(self, user: MongoDBUser) -> bool:
        return f"{user.get_username()}-user-created" in self.app_peer_data

    def _set_user_created(self, user: MongoDBUser) -> None:
        self.app_peer_data[f"{user.get_username()}-user-created"] = "True"

    def _get_mongodb_config_for_user(
        self, user: MongoDBUser, hosts: List[str]
    ) -> MongoDBConfiguration:
        external_ca, _ = self.tls.get_tls_files(UNIT_SCOPE)
        internal_ca, _ = self.tls.get_tls_files(APP_SCOPE)
        password = self.get_secret(APP_SCOPE, user.get_password_key_name())
        if not password:
            raise MissingSecretError(
                "Password for {APP_SCOPE}, {user.get_username()} couldn't be retrieved"
            )
        else:
            return MongoDBConfiguration(
                replset=self.app.name,
                database=user.get_database_name(),
                username=user.get_username(),
                password=password,  # type: ignore
                hosts=set(hosts),
                roles=set(user.get_roles()),
                tls_external=external_ca is not None,
                tls_internal=internal_ca is not None,
            )

    def _get_user_or_fail_event(self, event: ActionEvent, default_username: str) -> Optional[str]:
        """Returns MongoDBUser object or raises ActionFail if user doesn't exist."""
        username = event.params.get(Config.Actions.USERNAME_PARAM_NAME, default_username)
        if username not in CHARM_USERS:
            event.fail(
                f"The action can be run only for users used by the charm:"
                f" {', '.join(CHARM_USERS)} not {username}"
            )
            return
        return username

    def _check_or_set_user_password(self, user: MongoDBUser) -> None:
        key = user.get_password_key_name()
        if not self.get_secret(APP_SCOPE, key):
            self.set_secret(APP_SCOPE, key, generate_password())

    def _check_or_set_keyfile(self) -> None:
        if not self.get_secret(APP_SCOPE, "keyfile"):
            self._generate_keyfile()

    def _generate_keyfile(self) -> None:
        self.set_secret(APP_SCOPE, "keyfile", generate_keyfile())

    def _generate_secrets(self) -> None:
        """Generate passwords and put them into peer relation.

        The same keyFile and operator password on all members are needed.
        It means it is needed to generate them once and share between members.
        NB: only leader should execute this function.
        """
        self._check_or_set_user_password(OperatorUser)
        self._check_or_set_user_password(MonitorUser)

        self._check_or_set_keyfile()

    def _update_app_relation_data(self, database_users: Set[str]) -> None:
        """Helper function to update application relation data."""
        for relation in self.model.relations[Config.Relations.NAME]:
            username = self.client_relations._get_username_from_relation_id(relation.id)
            password = relation.data[self.app][Config.Actions.PASSWORD_PARAM_NAME]
            if username in database_users:
                config = self.client_relations._get_config(username, password)
                relation.data[self.app].update(
                    {
                        "endpoints": ",".join(config.hosts),
                        "uris": config.uri,
                    }
                )

    def _initialise_replica_set(self, event: StartEvent) -> None:
        """Initialise replica set and create users."""
        if self.db_initialised:
            # The replica set should be initialised only once. Check should be
            # external (e.g., check initialisation inside peer relation). We
            # shouldn't rely on MongoDB response because the data directory
            # can be corrupted.
            return

        # only leader should initialise the replica set
        if not self.unit.is_leader():
            return

        with MongoDBConnection(self.mongodb_config, "localhost", direct=True) as direct_mongo:
            try:
                logger.info("Replica Set initialization")
                direct_mongo.init_replset()
                logger.info("User initialization")
                self._init_operator_user()
                self._init_backup_user()
                self._init_monitor_user()
                logger.info("Reconcile relations")
                self.client_relations.oversee_users(None, event)
            except ExecError as e:
                logger.error(
                    "Deferring on_start: exit code: %i, stderr: %s", e.exit_code, e.stderr
                )
                event.defer()
                return
            except PyMongoError as e:
                logger.error("Deferring on_start since: error=%r", e)
                event.defer()
                return

            self.db_initialised = True

    def _add_units_from_replica_set(
        self, event, mongo: MongoDBConnection, units_to_add: Set[str]
    ) -> None:
        for member in units_to_add:
            logger.debug("Adding %s to the replica set", member)
            with MongoDBConnection(self.mongodb_config, member, direct=True) as direct_mongo:
                if not direct_mongo.is_ready:
                    logger.debug("Deferring reconfigure: %s is not ready yet.", member)
                    event.defer()
                    return
            mongo.add_replset_member(member)

    def _remove_units_from_replica_set(
        self, evemt, mongo: MongoDBConnection, units_to_remove: Set[str]
    ) -> None:
        for member in units_to_remove:
            logger.debug("Removing %s from the replica set", member)
            mongo.remove_replset_member(member)

    def _set_leader_unit_active_if_needed(self):
        # This can happen after restart mongod when enable \ disable TLS
        if (
            isinstance(self.unit.status, WaitingStatus)
            and self.unit.status.message == "waiting to reconfigure replica set"
        ):
            self.unit.status = ActiveStatus()

    def _juju_secrets_get(self, scope: Scopes) -> Optional[bool]:
        """Helper function to get Juju secret."""
        peer_data = self._peer_data(scope)

        if not peer_data.get(Config.Secrets.SECRET_INTERNAL_LABEL):
            return

        if Config.Secrets.SECRET_CACHE_LABEL not in self.secrets[scope]:
            try:
                # NOTE: Secret contents are not yet available!
                secret = self.model.get_secret(id=peer_data[Config.Secrets.SECRET_INTERNAL_LABEL])
            except SecretNotFoundError as e:
                logging.debug(
                    f"No secret found for ID {peer_data[Config.Secrets.SECRET_INTERNAL_LABEL]}, {e}"
                )
                return

            logging.debug(f"Secret {peer_data[Config.Secrets.SECRET_INTERNAL_LABEL]} downloaded")

            # We keep the secret object around -- needed when applying modifications
            self.secrets[scope][Config.Secrets.SECRET_LABEL] = secret

            # We retrieve and cache actual secret data for the lifetime of the event scope
            self.secrets[scope][Config.Secrets.SECRET_CACHE_LABEL] = secret.get_content()

        if self.secrets[scope].get(Config.Secrets.SECRET_CACHE_LABEL):
            return True
        return False

    def _juju_secret_get_key(self, scope: Scopes, key: str) -> Optional[str]:
        if not key:
            return

        if self._juju_secrets_get(scope):
            secret_cache = self.secrets[scope].get(Config.Secrets.SECRET_CACHE_LABEL)
            if secret_cache:
                secret_data = secret_cache.get(key)
                if secret_data and secret_data != Config.Secrets.SECRET_DELETED_LABEL:
                    logging.debug(f"Getting secret {scope}:{key}")
                    return secret_data
        logging.debug(f"No value found for secret {scope}:{key}")

    def get_secret(self, scope: Scopes, key: str) -> Optional[str]:
        """Getting a secret."""
        peer_data = self._peer_data(scope)

        juju_version = JujuVersion.from_environ()

        if juju_version.has_secrets:
            return self._juju_secret_get_key(scope, key)
        else:
            return peer_data.get(key)

    def _juju_secret_set(self, scope: Scopes, key: str, value: str) -> str:
        """Helper function setting Juju secret."""
        peer_data = self._peer_data(scope)
        self._juju_secrets_get(scope)

        secret = self.secrets[scope].get(Config.Secrets.SECRET_LABEL)

        # It's not the first secret for the scope, we can re-use the existing one
        # that was fetched in the previous call
        if secret:
            secret_cache = self.secrets[scope][Config.Secrets.SECRET_CACHE_LABEL]

            if secret_cache.get(key) == value:
                logging.debug(f"Key {scope}:{key} has this value defined already")
            else:
                secret_cache[key] = value
                try:
                    secret.set_content(secret_cache)
                except OSError as error:
                    logging.error(
                        f"Error in attempt to set {scope}:{key}. "
                        f"Existing keys were: {list(secret_cache.keys())}. {error}"
                    )
                logging.debug(f"Secret {scope}:{key} was {key} set")

        # We need to create a brand-new secret for this scope
        else:
            scope_obj = self._scope_opj(scope)

            secret = scope_obj.add_secret({key: value})
            if not secret:
                raise SecretNotAddedError(f"Couldn't set secret {scope}:{key}")

            self.secrets[scope][Config.Secrets.SECRET_LABEL] = secret
            self.secrets[scope][Config.Secrets.SECRET_CACHE_LABEL] = {key: value}
            logging.debug(f"Secret {scope}:{key} published (as first). ID: {secret.id}")
            peer_data.update({Config.Secrets.SECRET_INTERNAL_LABEL: secret.id})

        return self.secrets[scope][Config.Secrets.SECRET_LABEL].id

    def set_secret(self, scope: Scopes, key: str, value: Optional[str]) -> Optional[str]:
        """(Re)defining a secret."""
        if not value:
            return self.remove_secret(scope, key)

        juju_version = JujuVersion.from_environ()

        result = None
        if juju_version.has_secrets:
            result = self._juju_secret_set(scope, key, value)
        else:
            peer_data = self._peer_data(scope)
            peer_data.update({key: value})

        return result

    def _juju_secret_remove(self, scope: Scopes, key: str) -> None:
        """Remove a Juju 3.x secret."""
        self._juju_secrets_get(scope)

        secret = self.secrets[scope].get(Config.Secrets.SECRET_LABEL)
        if not secret:
            logging.error(f"Secret {scope}:{key} wasn't deleted: no secrets are available")
            return

        secret_cache = self.secrets[scope].get(Config.Secrets.SECRET_CACHE_LABEL)
        if not secret_cache or key not in secret_cache:
            logging.error(f"No secret {scope}:{key}")
            return

        secret_cache[key] = Config.Secrets.SECRET_DELETED_LABEL
        secret.set_content(secret_cache)
        logging.debug(f"Secret {scope}:{key}")

    def remove_secret(self, scope, key) -> None:
        """Removing a secret."""
        juju_version = JujuVersion.from_environ()
        if juju_version.has_secrets:
            return self._juju_secret_remove(scope, key)

        peer_data = self._peer_data(scope)
        del peer_data[key]

    def restart_mongod_service(self):
        """Restart mongod service."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        container.stop(Config.SERVICE_NAME)

        container.add_layer("mongod", self._mongod_layer, combine=True)
        container.replan()

        self._connect_mongodb_exporter()
        self._connect_pbm_agent()

    def _push_keyfile_to_workload(self, container: Container) -> None:
        """Upload the keyFile to a workload container."""
        keyfile = self.get_secret(APP_SCOPE, Config.Secrets.SECRET_KEYFILE_NAME)
        if not keyfile:
            raise MissingSecretError(f"No secret defined for {APP_SCOPE}, keyfile")
        else:
            container.push(
                Config.CONF_DIR + "/" + Config.TLS.KEY_FILE_NAME,
                keyfile,  # type: ignore
                make_dirs=True,
                permissions=0o400,
                user=Config.UNIX_USER,
                group=Config.UNIX_GROUP,
            )

    def push_tls_certificate_to_workload(self) -> None:
        """Uploads certificate to the workload container."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        external_ca, external_pem = self.tls.get_tls_files(UNIT_SCOPE)

        if external_ca is not None:
            logger.debug("Uploading external ca to workload container")
            container.push(
                Config.CONF_DIR + "/" + Config.TLS.EXT_CA_FILE,
                external_ca,
                make_dirs=True,
                permissions=0o400,
                user=Config.UNIX_USER,
                group=Config.UNIX_GROUP,
            )
        if external_pem is not None:
            logger.debug("Uploading external pem to workload container")
            container.push(
                Config.CONF_DIR + "/" + Config.TLS.EXT_PEM_FILE,
                external_pem,
                make_dirs=True,
                permissions=0o400,
                user=Config.UNIX_USER,
                group=Config.UNIX_GROUP,
            )

        internal_ca, internal_pem = self.tls.get_tls_files(APP_SCOPE)
        if internal_ca is not None:
            logger.debug("Uploading internal ca to workload container")
            container.push(
                Config.CONF_DIR + "/" + Config.TLS.INT_CA_FILE,
                internal_ca,
                make_dirs=True,
                permissions=0o400,
                user=Config.UNIX_USER,
                group=Config.UNIX_GROUP,
            )
        if internal_pem is not None:
            logger.debug("Uploading internal pem to workload container")
            container.push(
                Config.CONF_DIR + "/" + Config.TLS.INT_PEM_FILE,
                internal_pem,
                make_dirs=True,
                permissions=0o400,
                user=Config.UNIX_USER,
                group=Config.UNIX_GROUP,
            )

    def delete_tls_certificate_from_workload(self) -> None:
        """Deletes certificate from the workload container."""
        logger.info("Deleting TLS certificate from workload container")
        container = self.unit.get_container(Config.CONTAINER_NAME)
        for file in [
            Config.TLS.EXT_CA_FILE,
            Config.TLS.EXT_PEM_FILE,
            Config.TLS.INT_CA_FILE,
            Config.TLS.INT_PEM_FILE,
        ]:
            try:
                container.remove_path(f"{Config.CONF_DIR}/{file}")
            except PathError as err:
                logger.debug("Path unavailable: %s (%s)", file, str(err))

    def get_hostname_for_unit(self, unit: Unit) -> str:
        """Create a DNS name for a MongoDB unit.

        Args:
            unit_name: the juju unit name, e.g. "mongodb/1".

        Returns:
            A string representing the hostname of the MongoDB unit.
        """
        unit_id = unit.name.split("/")[1]
        return f"{self.app.name}-{unit_id}.{self.app.name}-endpoints"

    def _connect_mongodb_exporter(self) -> None:
        """Exposes the endpoint to mongodb_exporter."""
        container = self.unit.get_container(Config.CONTAINER_NAME)

        if not container.can_connect():
            return

        if not self.db_initialised:
            return

        # must wait for leader to set URI before connecting
        if not self.get_secret(APP_SCOPE, MonitorUser.get_password_key_name()):
            return

        current_service_config = (
            container.get_plan().to_dict().get("services", {}).get("mongodb_exporter", {})
        )
        new_service_config = self._monitor_layer.services.get("mongodb_exporter", {})

        if current_service_config == new_service_config:
            return

        # Add initial Pebble config layer using the Pebble API
        # mongodb_exporter --mongodb.uri=

        container.add_layer("mongodb_exporter", self._monitor_layer, combine=True)
        # Restart changed services and start startup-enabled services.
        container.replan()

    def _connect_pbm_agent(self) -> None:
        """Updates URI for pbm-agent."""
        container = self.unit.get_container(Config.CONTAINER_NAME)

        if not container.can_connect():
            return

        if not self.db_initialised:
            return

        # must wait for leader to set URI before any attempts to update are made
        if not self.get_secret("app", BackupUser.get_password_key_name()):
            return

        current_service_config = (
            container.get_plan().to_dict().get("services", {}).get(Config.Backup.SERVICE_NAME, {})
        )
        new_service_config = self._backup_layer.services.get(Config.Backup.SERVICE_NAME, {})

        if current_service_config == new_service_config:
            return

        container.add_layer(Config.Backup.SERVICE_NAME, self._backup_layer, combine=True)
        container.replan()

    def get_backup_service(self) -> ServiceInfo:
        """Returns the backup service."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        return container.get_service(Config.Backup.SERVICE_NAME)

    def is_unit_in_replica_set(self) -> bool:
        """Check if the unit is in the replica set."""
        with MongoDBConnection(self.mongodb_config) as mongo:
            try:
                replset_members = mongo.get_replset_members()
                return self.get_hostname_for_unit(self.unit) in replset_members
            except NotReadyError as e:
                logger.error(f"{self.unit.name}.is_unit_in_replica_set NotReadyError={e}")
            except PyMongoError as e:
                logger.error(f"{self.unit.name}.is_unit_in_replica_set PyMongoError={e}")
        return False

    def run_pbm_command(self, cmd: List[str]) -> str:
        """Executes a command in the workload container."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        environment = {"PBM_MONGODB_URI": f"{self.backup_config.uri}"}
        process = container.exec([Config.Backup.PBM_PATH] + cmd, environment=environment)
        stdout, _ = process.wait_output()
        return stdout

    def set_pbm_config_file(self) -> None:
        """Sets the pbm config file."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        container.push(
            Config.Backup.PBM_CONFIG_FILE_PATH,
            "# this file is to be left empty. Changes in this file will be ignored.\n",
            make_dirs=True,
            permissions=0o400,
        )
        try:
            self.run_pbm_command(
                [
                    "config",
                    "--file",
                    Config.Backup.PBM_CONFIG_FILE_PATH,
                ]
            )
        except ExecError as e:
            logger.error(f"Failed to set pbm config file. {e}")
            self.unit.status = BlockedStatus(process_pbm_error(e.stdout))
        return

    def start_backup_service(self) -> None:
        """Starts the backup service."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        container.start(Config.Backup.SERVICE_NAME)

    def restart_backup_service(self) -> None:
        """Restarts the backup service."""
        container = self.unit.get_container(Config.CONTAINER_NAME)
        container.restart(Config.Backup.SERVICE_NAME)

    # END: helper functions

    # BEGIN: static methods
    @staticmethod
    def _pull_licenses(container: Container) -> None:
        """Pull licences from workload."""
        licenses = [
            "snap",
            "rock",
            "mongodb-exporter",
            "percona-backup-mongodb",
            "percona-server",
        ]

        for license_name in licenses:
            try:
                license_file = container.pull(path=Config.get_license_path(license_name))
                f = open("LICENSE", "x")
                f.write(str(license_file.read()))
                f.close()
            except FileExistsError:
                pass

    @staticmethod
    def _set_data_dir_permissions(container: Container) -> None:
        """Ensure the data directory for mongodb is writable for the "mongodb" user.

        Until the ability to set fsGroup and fsGroupChangePolicy via Pod securityContext
        is available, we fix permissions incorrectly with chown.
        """
        paths = container.list_files(Config.DATA_DIR, itself=True)
        assert len(paths) == 1, "list_files doesn't return only the directory itself"
        logger.debug(f"Data directory ownership: {paths[0].user}:{paths[0].group}")
        if paths[0].user != Config.UNIX_USER or paths[0].group != Config.UNIX_GROUP:
            container.exec(
                f"chown {Config.UNIX_USER}:{Config.UNIX_GROUP} -R {Config.DATA_DIR}".split(" ")
            )

    # END: static methods


if __name__ == "__main__":
    main(MongoDBCharm)
