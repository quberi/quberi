from minio import Minio
from urllib3 import PoolManager, Retry, Timeout

from quberi_api.core.config import Settings


def create_object_storage_client(settings: Settings) -> Minio:
    http_client = PoolManager(
        timeout=Timeout(
            connect=settings.health_timeout_seconds,
            read=settings.health_timeout_seconds,
        ),
        retries=Retry(total=0),
    )
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        secure=settings.minio_secure,
        http_client=http_client,
    )
