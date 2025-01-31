"""Configuration for MongoDB Charm."""
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import Literal


class Config:
    """Configuration for MongoDB Charm."""

    SUBSTRATE = "k8s"
    MONGODB_PORT = 27017
    UNIX_USER = "mongodb"
    UNIX_GROUP = "mongodb"
    DATA_DIR = "/var/lib/mongodb"
    CONF_DIR = "/etc/mongod"
    MONGODB_LOG_FILENAME = "mongodb.log"
    LOG_FILES = [f"{DATA_DIR}/{MONGODB_LOG_FILENAME}"]
    LICENSE_PATH = "/licenses/LICENSE"
    CONTAINER_NAME = "mongod"
    SERVICE_NAME = "mongod"
    SOCKET_PATH = "/tmp/mongodb-27017.sock"
    APP_SCOPE = "app"
    UNIT_SCOPE = "unit"

    class Actions:
        """Actions related config for MongoDB Charm."""

        PASSWORD_PARAM_NAME = "password"
        USERNAME_PARAM_NAME = "username"

    class Backup:
        """Backup related config for MongoDB Charm."""

        SERVICE_NAME = "pbm-agent"
        URI_PARAM_NAME = "pbm-uri"
        PBM_PATH = "/usr/bin/pbm"
        PBM_CONFIG_FILE_PATH = "/etc/pbm_config.yaml"

    class Monitoring:
        """Monitoring related config for MongoDB Charm."""

        MONGODB_EXPORTER_PORT = 9216
        METRICS_ENDPOINTS = [
            {"path": "/metrics", "port": f"{MONGODB_EXPORTER_PORT}"},
        ]
        METRICS_RULES_DIR = "./src/alert_rules/prometheus"
        LOGS_RULES_DIR = "./src/alert_rules/loki"
        LOG_SLOTS = ["charmed-mongodb:logs"]
        URI_PARAM_NAME = "monitor-uri"
        SERVICE_NAME = "mongodb-exporter"
        JOBS = [{"static_configs": [{"targets": [f"*:{MONGODB_EXPORTER_PORT}"]}]}]
        APP_SCOPE = "app"
        UNIT_SCOPE = "unit"

    class Relations:
        """Relations related config for MongoDB Charm."""

        APP_SCOPE = "app"
        UNIT_SCOPE = "unit"
        NAME = "database"
        PEERS = "database-peers"
        LOGGING = "logging"

        Scopes = Literal[APP_SCOPE, UNIT_SCOPE]

    class TLS:
        """TLS related config for MongoDB Charm."""

        EXT_PEM_FILE = "external-cert.pem"
        EXT_CA_FILE = "external-ca.crt"
        INT_PEM_FILE = "internal-cert.pem"
        INT_CA_FILE = "internal-ca.crt"
        KEY_FILE_NAME = "keyFile"
        TLS_PEER_RELATION = "certificates"

        SECRET_CA_LABEL = "ca-secret"
        SECRET_KEY_LABEL = "key-secret"
        SECRET_CERT_LABEL = "cert-secret"
        SECRET_CSR_LABEL = "csr-secret"
        SECRET_CHAIN_LABEL = "chain-secret"

    class Secrets:
        """Secrets related constants."""

        SECRET_LABEL = "secret"
        SECRET_CACHE_LABEL = "cache"
        SECRET_KEYFILE_NAME = "keyfile"
        SECRET_INTERNAL_LABEL = "internal-secret"
        SECRET_DELETED_LABEL = "None"
        SECRET_KEYFILE_NAME = "keyfile"

    @staticmethod
    def get_license_path(license_name: str) -> str:
        """Return the path to the license file."""
        return f"{Config.LICENSE_PATH}-{license_name}"
