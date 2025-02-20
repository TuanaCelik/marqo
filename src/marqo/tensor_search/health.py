import math
import copy
from typing import Optional, Tuple

from marqo import errors
from marqo._httprequests import HttpRequests
from marqo.config import Config
from marqo.tensor_search.tensor_search_logging import get_logger
from marqo.tensor_search.enums import HealthStatuses
from marqo.tensor_search import constants
from marqo.tensor_search import validation

logger = get_logger(__name__)


def convert_watermark_to_bytes(watermark: str, total_in_bytes: int = None) -> int:
    """
    Converts a value to bytes.
    It could possibly be:
    1. Bytes (eg 1234b) - do nothing
    2. MB, GB, TB, etc, (eg 123.4gb) - multiply by some power of 1024 to get bytes
    3. Ratio (e.g. 0.9) - multiply by total_in_bytes to get bytes
    4. Percentage (e.g. 90%) - convert to ratio then multiply by total_in_bytes to get bytes

    Returns: watermark in bytes (int)
    """

    # Initial validation
    if watermark is None:
        raise errors.InternalError("OpenSearch disk watermark cannot be None.")
    watermark = watermark.replace(" ", "")
    if watermark == "":
        raise errors.InternalError("OpenSearch disk watermark cannot be empty string.")
    
    if total_in_bytes is not None and total_in_bytes < 0:
        raise errors.InternalError("OpenSearch cluster stats fs: total_in_bytes cannot be negative.")

    if watermark[-2:].lower() in constants.BYTE_SUFFIX_EXPONENTS:
        # Watermark in KB/MB/GB/TB format
        # Bytes represent MIN disk space AVAILABLE
        numeric_watermark = validation.validate_nonnegative_number(watermark[:-2], "OpenSearch disk watermark value")
        multiplier = 1024 ** constants.BYTE_SUFFIX_EXPONENTS[watermark[-2:].lower()]
        return numeric_watermark * multiplier
        
    elif watermark[-1].lower() == "b":
        # Watermark in BYTE format
        # Bytes represent MIN disk space AVAILABLE
        numeric_watermark = validation.validate_nonnegative_number(watermark[:-1], "OpenSearch disk watermark value")
        return numeric_watermark
    
    # Percentage or Ratio calculation
    if watermark[-1] == "%":
        # Watermark in PERCENTAGE format
        # Ratio & percentage represent MAX disk space USED
        numeric_watermark = validation.validate_nonnegative_number(watermark[:-1], "OpenSearch disk watermark value")
        ratio_watermark = (100 - numeric_watermark) / 100
    else:
        # Watermark in RATIO format
        # Ratio & percentage represent MAX disk space USED
        numeric_watermark = validation.validate_nonnegative_number(watermark, "OpenSearch disk watermark value")
        ratio_watermark = 1 - numeric_watermark

    if ratio_watermark < 0 or ratio_watermark > 1:
        raise errors.InternalError("OpenSearch watermark ratio or percentage cannot be negative or more than 100%.")
    if total_in_bytes is None:
        raise errors.InternalError("total_in_bytes must be provided for ratio or percentage watermark.")
    
    # Round up to the next byte (more conservative approach)
    return math.ceil(total_in_bytes * ratio_watermark)


def check_opensearch_disk_watermark_breach(config: Config):
    """
    Checks if the OpenSearch disk watermark is breached:
    1. Check disk watermark from the settings endpoint.
      - Check transient, persistent, then default settings
      - convert it to a size in BYTES (it could initially be a percentage, ratio, or size in B, GB, MB, etc.)
    2. Check the current available disk space from the stats endpoint.
    3. Compare current avilable space to watermark value.

    Returns: yellow if watermark is breached, green otherwise.
    """

    # Query opensearch for watermark
    raw_flood_stage_watermark = None
    opensearch_settings = HttpRequests(config).get(path="_cluster/settings?include_defaults=true&filter_path=**.disk*")
    for settings_type in constants.OPENSEARCH_CLUSTER_SETTINGS_TYPES:
        try:
            # Check for transient, persistent, then defaults settings
            opensearch_disk_settings = opensearch_settings[settings_type]["cluster"]["routing"]["allocation"]["disk"]
            raw_flood_stage_watermark = opensearch_disk_settings["watermark"]["flood_stage"]
            logger.debug(f"Found disk flood stage watermark in {settings_type} settings: {raw_flood_stage_watermark}")
            break
        except KeyError:
            pass
    
    if not raw_flood_stage_watermark:
        raise errors.InternalError("Could not find disk flood stage watermark in OpenSearch settings.")
    # Query opensearch for disk space
    filesystem_stats = HttpRequests(config).get(path="_cluster/stats")["nodes"]["fs"]
    minimum_available_disk_space = convert_watermark_to_bytes(watermark=raw_flood_stage_watermark, total_in_bytes=filesystem_stats["total_in_bytes"])
    if filesystem_stats["available_in_bytes"] <= minimum_available_disk_space:
        return HealthStatuses.yellow
    return HealthStatuses.green


def generate_heath_check_response(config: Config, index_name: Optional[str] = None) -> dict:
    """Generate the health check response for check_heath(), check_index_health() APIs in tensor_search"""
    marqo_status = get_marqo_status()
    marqo_os_status, marqo_os_storage_is_available = get_marqo_os_status(config, index_name=index_name)
    aggregated_marqo_status, marqo_os_status = aggregate_status(marqo_status, marqo_os_status)

    return {
        "status": aggregated_marqo_status.value,
        "backend": {
            "status": marqo_os_status.value,
            "storage_is_available": marqo_os_storage_is_available
        }
    }


def get_marqo_status() -> HealthStatuses:
    """Check the Marqo instance status."""
    return HealthStatuses.green


def get_marqo_os_status(config: Config, index_name: Optional[str] = None) -> Tuple[HealthStatuses, bool]:
    """
    Check the Marqo-os backend status.

    3 OpenSearch calls are made in this check:
    1. health: _cluster/health or _cluster/health/{index_name}
    2. settings: _cluster/settings?include_defaults=true&filter_path=**.disk*
    3. stats: _cluster/stats
    """
    TIMEOUT = 3
    marqo_os_health_check_response = None
    path = f"_cluster/health/{index_name}" if index_name else "_cluster/health"

    try:
        timeout_config = copy.deepcopy(config)
        timeout_config.timeout = TIMEOUT
        marqo_os_health_check_response = HttpRequests(timeout_config).get(path=path)
    except errors.InternalError:
        marqo_os_health_check_response = None

    if marqo_os_health_check_response is not None:
        if "status" in marqo_os_health_check_response and marqo_os_health_check_response['status'] \
                in list(HealthStatuses):
            marqo_os_status = HealthStatuses[marqo_os_health_check_response['status']]
        else:
            marqo_os_status = HealthStatuses.red
    else:
        marqo_os_status = HealthStatuses.red

    marqo_os_disk_watermark_breached = check_opensearch_disk_watermark_breach(config)
    
    # Storage is available if disk watermark is not breached (green).
    if marqo_os_disk_watermark_breached == HealthStatuses.green:
        marqo_os_storage_is_available = True
    else:
        marqo_os_storage_is_available = False
    
    marqo_os_status = max(marqo_os_status, marqo_os_disk_watermark_breached)
    return marqo_os_status, marqo_os_storage_is_available


def aggregate_status(marqo_status: HealthStatuses, marqo_os_status: HealthStatuses) \
        -> Tuple[HealthStatuses, HealthStatuses]:
    """Aggregate the Marqo instance and Marqo-os backend status."""
    aggregated_marqo_status = max(marqo_status, marqo_os_status)
    return aggregated_marqo_status, marqo_os_status
