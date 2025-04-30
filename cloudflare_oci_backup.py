#!/usr/bin/env python3

import requests
import oci
import os
import datetime
import logging
import sys
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv # Import dotenv
from datetime import timezone # <-- Import timezone

# --- Configuration ---
# Best practice: Load sensitive info like tokens and keys from environment variables
# or OCI config files, not hardcoded here.
CLOUDFLARE_API_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
OCI_BUCKET_NAME_ENV = "OCI_BUCKET_NAME" # Use standard env var naming convention
OCI_CONFIG_FILE = "~/.oci/config"
OCI_CONFIG_PROFILE = "DEFAULT"

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
        # Optionally add logging.FileHandler("backup.log")
    ]
)

# --- Function Definitions ---

def load_oci_config() -> Tuple[Optional[dict], Optional[str]]:
    """
    Loads OCI configuration from file and environment variables.

    Returns:
        A tuple containing the OCI config object and the bucket name,
        or (None, None) if loading fails.
    """
    logging.info("Loading OCI configuration...")
    oci_bucket_name = os.getenv(OCI_BUCKET_NAME_ENV)
    if not oci_bucket_name:
        logging.error(f"Environment variable '{OCI_BUCKET_NAME_ENV}' not set. This variable should contain your OCI bucket name.")
        return None, None

    try:
        # Expand user path for config file
        config_path = os.path.expanduser(OCI_CONFIG_FILE)
        if not os.path.exists(config_path):
             logging.error(f"OCI config file not found at: {config_path}")
             # Attempt to load default config or use instance principals if available
             logging.info("Attempting to load default OCI config or use Instance Principals...")
             try:
                 config = oci.config.from_file(profile_name=OCI_CONFIG_PROFILE) # Let it try default location
             except oci.exceptions.ConfigFileNotFound as e:
                 logging.warning(f"Default OCI config file not found ({e}). Trying Instance Principals...")
                 try:
                      signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                      config = {'signer': signer} # Create a dummy config with the signer
                      logging.info("Using Instance Principals for OCI authentication.")
                 except Exception as ip_error:
                     logging.error(f"Failed to use Instance Principals: {ip_error}")
                     return None, oci_bucket_name # Return bucket name even if config fails
        else:
            config = oci.config.from_file(file_location=config_path, profile_name=OCI_CONFIG_PROFILE)
        logging.info(f"Successfully loaded OCI configuration (profile: {OCI_CONFIG_PROFILE}).")
        return config, oci_bucket_name
    except oci.exceptions.ConfigFileNotFound:
        logging.error(f"OCI config file not found at {config_path}.")
    except oci.exceptions.InvalidConfig as e:
        logging.error(f"Invalid OCI config file: {e}")
    except KeyError:
        logging.error(f"Profile '{OCI_CONFIG_PROFILE}' not found in {config_path}.")
    except Exception as e:
        logging.error(f"Failed to load OCI config: {e}")

    # Fallback if file loading fails but Instance Principals might work
    try:
        logging.info("Attempting Instance Principals authentication as fallback...")
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {'signer': signer}
        logging.info("Using Instance Principals for OCI authentication.")
        return config, oci_bucket_name
    except Exception as ip_error:
        logging.error(f"Fallback to Instance Principals failed: {ip_error}")
        return None, oci_bucket_name # Still return bucket name if env var was set

def get_cloudflare_zones(api_token: str) -> Optional[List[Dict[str, str]]]:
    """
    Fetches the list of zones from the Cloudflare API.

    Args:
        api_token: The Cloudflare API token.

    Returns:
        A list of dictionaries, each containing 'id' and 'name' of a zone,
        or None if an error occurs.
    """
    logging.info("Fetching Cloudflare zone list...")
    headers = {"Authorization": f"Bearer {api_token}"}
    url = "https://api.cloudflare.com/client/v4/zones"
    zones = []
    page = 1
    while True:
        try:
            params = {"page": page, "per_page": 50} # Fetch 50 zones per page
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)

            data = response.json()
            if not data.get("success"):
                errors = data.get("errors", [{"message": "Unknown error"}])
                logging.error(f"Cloudflare API error fetching zones: {errors[0]['message']}")
                return None

            current_zones = data.get("result", [])
            zones.extend([{"id": z["id"], "name": z["name"]} for z in current_zones])

            # Check pagination
            result_info = data.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        except requests.exceptions.Timeout:
            logging.error("Timeout occurred while fetching Cloudflare zones.")
            return None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401 or e.response.status_code == 403:
                logging.error("Cloudflare API authentication failed (Invalid Token or Permissions?).")
            elif e.response.status_code == 429:
                logging.error("Cloudflare API rate limit exceeded.")
                # Basic retry/backoff could be added here
            else:
                logging.error(f"HTTP error fetching Cloudflare zones: {e}")
            return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error fetching Cloudflare zones: {e}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred fetching zones: {e}")
            return None

    logging.info(f"Found {len(zones)} Cloudflare zones.")
    return zones

def export_zone_dns(zone_id: str, api_token: str) -> Optional[str]:
    """
    Exports DNS records for a specific zone from Cloudflare in BIND format.

    Args:
        zone_id: The ID of the Cloudflare zone.
        api_token: The Cloudflare API token.

    Returns:
        The DNS records in BIND format as a string, or None on error.
    """
    logging.info(f"Exporting DNS for zone ID: {zone_id}...")
    headers = {"Authorization": f"Bearer {api_token}"}
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/export"
    try:
        response = requests.get(url, headers=headers, timeout=60) # Longer timeout for potentially large exports
        response.raise_for_status()
        # Cloudflare export returns plain text (BIND format) on success
        logging.info(f"Successfully exported DNS for zone ID: {zone_id}")
        return response.text
    except requests.exceptions.Timeout:
        logging.error(f"Timeout occurred while exporting DNS for zone ID: {zone_id}.")
        return None
    except requests.exceptions.HTTPError as e:
         if e.response.status_code == 401 or e.response.status_code == 403:
            logging.error(f"Cloudflare API authentication failed exporting zone {zone_id}.")
         elif e.response.status_code == 429:
             logging.error(f"Cloudflare API rate limit exceeded exporting zone {zone_id}.")
         else:
            logging.error(f"HTTP error exporting DNS for zone {zone_id}: {e}")
         return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error exporting DNS for zone {zone_id}: {e}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred exporting DNS for zone {zone_id}: {e}")
        return None


def generate_oci_object_name(zone_name: str) -> str:
    """
    为 OCI 存储生成基于区域名称和精确时间的对象名称。

    Args:
        zone_name: Cloudflare 区域的名称。

    Returns:
        生成的对象名称字符串，格式为 cloudflare-DNS-backups/YYYY-MM-DD-HHMM/zone_name.bind。
    """
    # 对区域名称进行清理以确保安全，尽管对于域名通常不是必需的
    safe_zone_name = zone_name.replace("/", "_")
    # 使用当前UTC时间生成包含小时和分钟的时间戳
    now_utc = datetime.datetime.now(timezone.utc)
    timestamp = now_utc.strftime("%Y-%m-%d-%H%M") # YYYY-MM-DD-HHMM 格式
    return f"cloudflare-DNS-backups/{timestamp}/{safe_zone_name}.bind"

def upload_to_oci_object_storage(
    object_storage_client: oci.object_storage.ObjectStorageClient,
    namespace: str,
    bucket_name: str,
    object_name: str,
    data_bytes: bytes
) -> bool:
    """
    Uploads byte data to OCI Object Storage.

    Args:
        object_storage_client: Initialized OCI ObjectStorageClient.
        namespace: The OCI Object Storage namespace.
        bucket_name: The target OCI bucket name.
        object_name: The desired name for the object in the bucket.
        data_bytes: The data to upload as bytes.

    Returns:
        True if upload was successful, False otherwise.
    """
    logging.info(f"Uploading {object_name} to OCI bucket {bucket_name}...")
    try:
        object_storage_client.put_object(
            namespace_name=namespace,
            bucket_name=bucket_name,
            object_name=object_name,
            put_object_body=data_bytes
        )
        logging.info(f"Successfully uploaded {object_name} to OCI.")
        return True
    except oci.exceptions.ServiceError as e:
        logging.error(
            f"OCI Service Error uploading {object_name}: "
            f"Status={e.status}, Code={e.code}, Message={e.message}"
        )
        if e.status == 404 and e.code == "BucketNotFound":
             logging.error(f"Bucket '{bucket_name}' not found in namespace '{namespace}'.")
        elif e.status == 404 and e.code == "NamespaceNotFound":
             logging.error(f"Namespace '{namespace}' not found.")
        elif e.status == 401 or e.status == 403:
             logging.error("OCI Authentication/Authorization error. Check config and IAM policies (need OBJECT_CREATE).")
    except oci.exceptions.RequestException as e:
         logging.error(f"OCI Network/Connection error uploading {object_name}: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during OCI upload of {object_name}: {e}")

    return False


def apply_retention_policy(object_storage_client: oci.object_storage.ObjectStorageClient, namespace: str, bucket_name: str):
    """
    Applies the backup retention policy to the OCI bucket.

    根据以下规则清理 OCI 存储桶中的备份：
    - 保留最近 7 天 (7 * 24 小时) 内的所有备份。
    - 保留超过 7 天但在最近 1 个月内的每周最后一个备份 (按时间戳最新)。
    - 保留超过 1 个月但在最近 1 年内的每月最后一个备份 (按时间戳最新)。
    - 永久保留超过 1 年的每年最后一个备份 (按时间戳最新)。
    - 删除 'cloudflare-DNS-backups/' 前缀下所有不符合上述条件的对象。
    """
    logging.info(f"Applying retention policy to bucket: {bucket_name}")
    now_utc = datetime.datetime.now(timezone.utc) # 获取当前的 UTC 时间
    # 定义时间阈值 (使用 aware datetime 对象进行比较)
    seven_days_ago = now_utc - datetime.timedelta(days=7)
    # 使用近似天数，可以根据需要调整为更精确的日历月/年逻辑
    one_month_ago = now_utc - datetime.timedelta(days=31)
    one_year_ago = now_utc - datetime.timedelta(days=365)
    prefix = "cloudflare-DNS-backups/"

    try:
        # --- 1. 列出所有相关对象 ---
        logging.info(f"Listing objects with prefix '{prefix}'...")
        all_objects_raw = []
        next_start_with = None
        while True:
            try:
                list_response = object_storage_client.list_objects(
                    namespace,
                    bucket_name,
                    prefix=prefix,
                    start=next_start_with,
                    fields="name,timeCreated" # 获取名称和创建时间
                )
            except oci.exceptions.ServiceError as e:
                 logging.error(f"OCI Error listing objects: {e.status} {e.code} - {e.message}")
                 if e.status == 404:
                      logging.error(f"Bucket '{bucket_name}' or prefix '{prefix}' not found?")
                      logging.warning("Prefix not found, skipping retention policy.")
                      return
                 raise # 重新引发其他服务错误，如身份验证问题

            if not list_response.data or not hasattr(list_response.data, 'objects'):
                 logging.warning("No objects found or unexpected response format.")
                 break # 如果没有返回对象，则退出循环

            all_objects_raw.extend(list_response.data.objects)
            if not list_response.data.next_start_with:
                break
            next_start_with = list_response.data.next_start_with
            logging.debug(f"Fetched {len(list_response.data.objects)} objects, next start: {next_start_with}")

        logging.info(f"Found {len(all_objects_raw)} total raw objects matching prefix '{prefix}'.")

        # --- 2. 解析和过滤对象，按时间戳排序 ---
        backup_info_list = []
        parse_errors = 0
        for obj in all_objects_raw:
            # 检查对象是否有有效的名称和 timeCreated 属性
            if not hasattr(obj, 'name') or not obj.name or not hasattr(obj, 'time_created') or not obj.time_created:
                logging.warning(f"Skipping object with missing name or timeCreated: {obj}")
                continue
            # 跳过潜在的目录标记
            if obj.name.endswith('/'):
                 logging.debug(f"Skipping potential directory marker: {obj.name}")
                 continue

            # 解析对象名称以确保它看起来像一个备份文件（可选，因为我们主要依赖 timeCreated）
            # 这有助于过滤掉可能位于同一前缀下的非备份文件
            parts = obj.name.split('/')
            # 格式: cloudflare-DNS-backups/YYYY-MM-DD-HHMM/zone_name.bind
            if len(parts) >= 3 and parts[0] == prefix.strip('/'):
                 # 可以在这里添加对 parts[1] (时间戳部分) 的更严格验证，但 timeCreated 是主要依据
                 backup_info_list.append((obj.time_created, obj.name)) # time_created 是 aware datetime
            else:
                 logging.warning(f"Skipping object with unexpected name format: {obj.name}")
                 parse_errors += 1

        if parse_errors > 0:
             logging.warning(f"Encountered {parse_errors} errors parsing object names (or unexpected format).")

        if not backup_info_list:
            logging.info("No valid backup objects found to apply retention policy.")
            return

        # 按时间戳降序排序 (最新的在前)
        backup_info_list.sort(key=lambda x: x[0], reverse=True)
        logging.info(f"Processing {len(backup_info_list)} valid backup objects, sorted by time.")


        # --- 3. 根据规则确定要保留的对象 ---
        keep_objects_names = set()      # 存储要保留的对象名称
        last_kept_dt_per_week = {}    # { (year, week): latest_datetime }
        last_kept_dt_per_month = {}   # { (year, month): latest_datetime }
        last_kept_dt_per_year = {}    # { year: latest_datetime }

        logging.info("Evaluating retention rules based on timeCreated...")
        for backup_dt, obj_name in backup_info_list:
            # backup_dt 是 aware datetime (UTC from OCI)
            logging.debug(f"Processing object: {obj_name} (Created: {backup_dt})")

            kept_by_rule = False # 标记此对象是否已被某个规则保留

            # 规则 1: 保留最近 7 天的所有备份 (与 now_utc 比较)
            if backup_dt >= seven_days_ago:
                logging.debug(f"  KEEP (Rule 1: <= 7 days old): {obj_name}")
                keep_objects_names.add(obj_name)
                kept_by_rule = True
                # 注意：即使在此处保留，也继续检查其他规则，因为它可能是某周/月/年的最后一个

            # --- 对于超过 7 天的对象，检查其他规则 ---
            year, week, _ = backup_dt.isocalendar()
            month = backup_dt.month
            year_only = backup_dt.year

            # 规则 2: 保留每周最后一个 (> 7 天, <= 1 个月)
            # (由于已排序，我们遇到的第一个给定周的对象就是该周最新的)
            if backup_dt < seven_days_ago and backup_dt >= one_month_ago:
                week_key = (year, week)
                if week_key not in last_kept_dt_per_week:
                    last_kept_dt_per_week[week_key] = backup_dt # 记录此周已找到最新的
                    if not kept_by_rule: # 只有当它还没被规则1保留时才添加
                       logging.debug(f"  KEEP (Rule 2: Last of week {week_key}, >7d, <=1mo): {obj_name}")
                       keep_objects_names.add(obj_name)
                       kept_by_rule = True
                    else:
                       logging.debug(f"  INFO (Rule 2 check): Already kept by Rule 1, but is last of week {week_key}: {obj_name}")


            # 规则 3: 保留每月最后一个 (> 1 个月, <= 1 年)
            if backup_dt < one_month_ago and backup_dt >= one_year_ago:
                month_key = (year, month)
                if month_key not in last_kept_dt_per_month:
                    last_kept_dt_per_month[month_key] = backup_dt
                    if not kept_by_rule:
                        logging.debug(f"  KEEP (Rule 3: Last of month {month_key}, >1mo, <=1y): {obj_name}")
                        keep_objects_names.add(obj_name)
                        kept_by_rule = True
                    else:
                        logging.debug(f"  INFO (Rule 3 check): Already kept, but is last of month {month_key}: {obj_name}")

            # 规则 4: 保留每年最后一个 (> 1 年)
            if backup_dt < one_year_ago:
                year_key = year_only
                if year_key not in last_kept_dt_per_year:
                    last_kept_dt_per_year[year_key] = backup_dt
                    if not kept_by_rule:
                        logging.debug(f"  KEEP (Rule 4: Last of year {year_key}, >1y): {obj_name}")
                        keep_objects_names.add(obj_name)
                        kept_by_rule = True
                    else:
                         logging.debug(f"  INFO (Rule 4 check): Already kept, but is last of year {year_key}: {obj_name}")

            if not kept_by_rule:
                 logging.debug(f"  DELETE Candidate (Not kept by any rule): {obj_name}")


        # --- 4. 确定要删除的对象 ---
        # 从所有有效备份对象中找出不在保留集合中的
        objects_to_delete_names = [
            name for dt, name in backup_info_list if name not in keep_objects_names
        ]

        kept_count = len(keep_objects_names)
        delete_count = len(objects_to_delete_names)
        total_valid_objects = len(backup_info_list)

        logging.info("--- Retention Policy Evaluation Summary ---")
        logging.info(f"Total valid objects processed: {total_valid_objects}")
        logging.info(f"Total objects listed from OCI: {len(all_objects_raw)}")
        logging.info(f"Objects to keep:       {kept_count}")
        logging.info(f"Objects to delete:     {delete_count}")

        # --- 5. Execute Deletion (with confirmation) ---
        if not objects_to_delete_names:
             logging.info("No objects marked for deletion based on retention policy.")
             return

        # Safety Check: Ask for confirmation if running interactively
        proceed = False
        try:
            if not sys.stdout.isatty():
                 logging.warning("Running in non-interactive mode. Automatic deletion proceeding.")
                 proceed = True
            else:
                 confirm = input(f"\n!!! WARNING !!!\nAbout to delete {delete_count} objects from bucket '{bucket_name}'.\nThis action CANNOT be undone.\nType 'yes' to proceed: ")
                 proceed = confirm.strip().lower() == 'yes'
        except EOFError: # Handle environments where input() fails
             logging.warning("EOFError reading input. Assuming non-interactive. Automatic deletion proceeding.")
             proceed = True

        if not proceed:
            logging.info("Deletion cancelled by user or skipped.")
            return

        logging.info(f"Proceeding with deletion of {delete_count} objects...")
        deleted_success_count = 0
        delete_error_count = 0
        for i, object_name in enumerate(objects_to_delete_names):
            try:
                logging.info(f"Deleting object {i+1}/{delete_count}: {object_name}")
                object_storage_client.delete_object(namespace, bucket_name, object_name)
                deleted_success_count += 1
            except oci.exceptions.ServiceError as e:
                logging.error(f"Failed to delete object {object_name}: {e.status} {e.code} - {e.message}")
                if e.status == 404:
                     logging.warning(f"Object {object_name} not found during deletion attempt (already deleted?).")
                else:
                     delete_error_count += 1 # Count significant errors
            except Exception as e:
                logging.error(f"Unexpected error deleting object {object_name}: {e}")
                delete_error_count += 1

        logging.info(f"--- Deletion Summary ---")
        logging.info(f"Successfully deleted: {deleted_success_count}")
        logging.info(f"Deletion errors:      {delete_error_count}")

        if delete_error_count > 0:
             # Decide if this should be a critical failure impacting exit code
             logging.error(f"Encountered {delete_error_count} errors during object deletion. Please review logs.")
             # raise Exception(f"Encountered {delete_error_count} errors during deletion.") # Optionally make it fail the script

    except oci.exceptions.ServiceError as e:
        logging.error(f"OCI Service Error during retention policy: {e.status} {e.code} - {e.message}")
        # Re-raise or handle based on desired script behavior on retention failure
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred during retention policy execution: {e}")
        import traceback
        logging.error(traceback.format_exc()) # Log stack trace for debugging
        # Re-raise or handle
        raise

# --- End of apply_retention_policy definition ---


def main():
    """
    Main execution function.
    """
    logging.info("--- Starting Cloudflare to OCI Backup Script ---")

    # 1. Load environment variables from .env file
    load_dotenv()
    logging.info("Loaded environment variables from .env file (if present).")

    # 2. Load OCI Configuration (reads OCI_BUCKET_NAME from environment)
    oci_config, oci_bucket_name = load_oci_config()
    # Check if OCI config loading failed (returned None)
    if oci_config is None:
        logging.error("OCI Configuration could not be loaded (returned None). Check previous logs for details. Exiting.")
        sys.exit(1)
    # Check if OCI Bucket Name is configured (retrieved from environment variable)
    if oci_bucket_name is None:
        # The error for the missing env var is already logged in load_oci_config
        # We just need to ensure we exit if it wasn't retrieved.
        logging.error(f"OCI Bucket Name not found (was environment variable '{OCI_BUCKET_NAME_ENV}' set?). Exiting.")
        sys.exit(1)


    # 3. Get Cloudflare Token (reads CLOUDFLARE_API_TOKEN from environment)
    cloudflare_token = os.getenv(CLOUDFLARE_API_TOKEN_ENV)
    if not cloudflare_token:
        logging.error(f"Cloudflare API Token environment variable '{CLOUDFLARE_API_TOKEN_ENV}' not set or empty. Exiting.")
        sys.exit(1)

    # 4. Initialize OCI Client
    try:
        # Handle both config file and signer-based authentication
        if 'signer' in oci_config:
            object_storage_client = oci.object_storage.ObjectStorageClient(config={}, signer=oci_config['signer'])
            logging.info("Initialized OCI client using signer (likely Instance Principals).")
        else:
             object_storage_client = oci.object_storage.ObjectStorageClient(oci_config)
             logging.info("Initialized OCI client using configuration file.")

        # 3. Get OCI Namespace
        oci_namespace = object_storage_client.get_namespace().data
        logging.info(f"Using OCI Namespace: {oci_namespace}")

    except oci.exceptions.MissingDependency as e:
        logging.error(f"OCI SDK dependency missing: {e}. Ensure 'oci' package is installed correctly.")
        sys.exit(1)
    except oci.exceptions.ConfigFileNotFound as e:
        # This might happen if Instance Principals failed and config file fallback also failed
        logging.error(f"OCI config file error after attempting fallback: {e}")
        sys.exit(1)
    except oci.exceptions.ServiceError as e:
        logging.error(f"OCI Service Error during initialization or getting namespace: {e.message} (Status: {e.status}, Code: {e.code})")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Failed to initialize OCI client or get namespace: {e}")
        sys.exit(1)


    # 5. Get Cloudflare Zones
    zones = get_cloudflare_zones(cloudflare_token)
    if zones is None:
        logging.error("Failed to retrieve Cloudflare zones. Exiting.")
        sys.exit(1)
    if not zones:
        logging.warning("No Cloudflare zones found for this account.")
        sys.exit(0)

    # 6. Process Each Zone
    success_count = 0
    failure_count = 0
    total_zones = len(zones)

    for i, zone in enumerate(zones):
        zone_id = zone["id"]
        zone_name = zone["name"]
        logging.info(f"Processing zone {i+1}/{total_zones}: {zone_name} ({zone_id})")

        dns_data = export_zone_dns(zone_id, cloudflare_token)

        if dns_data:
            try:
                dns_bytes = dns_data.encode('utf-8')
                oci_object_name = generate_oci_object_name(zone_name)

                if upload_to_oci_object_storage(
                    object_storage_client,
                    oci_namespace,
                    oci_bucket_name,
                    oci_object_name,
                    dns_bytes
                ):
                    success_count += 1
                else:
                    failure_count += 1
                    logging.error(f"Failed to upload backup for zone: {zone_name}")

            except UnicodeEncodeError as e:
                logging.error(f"Failed to encode DNS data to UTF-8 for zone {zone_name}: {e}")
                failure_count += 1
            except Exception as e:
                 logging.error(f"Unexpected error processing zone {zone_name}: {e}")
                 failure_count += 1
        else:
            failure_count += 1
            logging.error(f"Failed to export DNS for zone: {zone_name}")

    # 7. Final Summary
    logging.info("--- Backup Task Summary ---")
    logging.info(f"Total Zones Processed: {total_zones}")
    logging.info(f"Successful Backups:   {success_count}")
    logging.info(f"Failed Backups:       {failure_count}")
    logging.info("--- Backup Script Finished ---")

    # 8. Apply Retention Policy (only if all backups succeeded)
    if failure_count == 0:
        logging.info("--- Starting Backup Retention Policy Application ---")
        try:
            # Ensure necessary OCI client and config variables are available
            if 'object_storage_client' in locals() and 'oci_namespace' in locals() and 'oci_bucket_name' in locals():
                 apply_retention_policy(object_storage_client, oci_namespace, oci_bucket_name)
                 logging.info("--- Backup Retention Policy Application Finished ---")
            else:
                 # This should ideally not happen if backup stages passed, but it's a safeguard
                 logging.error("Required OCI client/config variables not initialized prior to retention step. Skipping retention policy.")
                 # Even though backups succeeded, retention couldn't run, maybe exit with a specific code?
                 # For now, we'll exit 0 as backups are safe.
                 sys.exit(0)

        except Exception as e:
            # Catch potential errors raised from the retention function itself
            logging.error(f"An error occurred during the retention policy execution: {e}")
            # Log the error, but since backups were successful, exit with 0.
            # Consider a different exit code (e.g., 2) to indicate partial success (backup ok, retention failed)
            logging.warning("Retention policy failed, but backups were successful. Exiting with code 0.")
            sys.exit(0)

        # If retention policy ran without raising an exception
        logging.info("Script finished successfully (including retention policy application).")
        sys.exit(0) # Explicit successful exit

    else:
        # Failures occurred during the backup phase
        logging.error("Skipping retention policy application due to errors during the backup phase.")
        logging.info("Script finished with backup errors.")
        sys.exit(1) # Exit with error code 1 because backups failed

# --- Entry Point ---
if __name__ == "__main__":
    main()