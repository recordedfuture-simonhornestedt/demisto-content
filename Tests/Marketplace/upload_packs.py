import json
import os
import re
import sys
import argparse
import shutil
import uuid
import prettytable
import glob
import requests
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile
from typing import Any

from requests import Response

from Tests.Marketplace.marketplace_services import init_storage_client, Pack, \
    load_json, get_content_git_client, get_recent_commits_data, store_successful_and_failed_packs_in_ci_artifacts, \
    json_write
from Tests.Marketplace.marketplace_statistics import StatisticsHandler
from Tests.Marketplace.marketplace_constants import PackStatus, Metadata, GCPConfig, BucketUploadFlow, \
    CONTENT_ROOT_PATH, PACKS_FOLDER, IGNORED_FILES, LANDING_PAGE_SECTIONS_PATH, SKIPPED_STATUS_CODES, XSOAR_MP, XSOAR_SAAS_MP
from demisto_sdk.commands.common.tools import str2bool, open_id_set_file
from demisto_sdk.commands.content_graph.interface.neo4j.neo4j_graph import Neo4jContentGraphInterface
from Tests.scripts.utils.log_util import install_logging
from Tests.scripts.utils import logging_wrapper as logging
import traceback
from Tests.Marketplace.pack_readme_handler import download_markdown_images_from_artifacts

METADATA_FILE_REGEX_GET_VERSION = r'metadata\-([\d\.]+)\.json'
TEST_XDR_PREFIX = os.getenv("TEST_XDR_PREFIX", "")  # for testing


def get_packs_ids_to_upload(packs_to_upload: str) -> set:
    """Returns a set of pack's names to upload.

    Args:
        packs_to_upload (str): csv list of packs names to upload.

    Returns:
        set: unique collection of packs names to upload.
    """
    if packs_to_upload and isinstance(packs_to_upload, str):
        packs = {p.strip() for p in packs_to_upload.split(',') if p not in IGNORED_FILES}
        logging.info(f"Collected {len(packs)} content packs to upload: {list(packs)}")
        return packs
    else:
        logging.critical("Not correct usage of flag -p. Please check help section of upload packs script.")
        sys.exit(1)


def extract_packs_artifacts(packs_artifacts_path: str, extract_destination_path: str):
    """Extracts all packs from content pack artifact zip.

    Args:
        packs_artifacts_path (str): full path to content artifacts zip file.
        extract_destination_path (str): full path to directory where to extract the packs.

    """
    with ZipFile(packs_artifacts_path) as packs_artifacts:
        packs_artifacts.extractall(extract_destination_path)
    logging.debug("Finished extracting packs artifacts")


def download_and_extract_index(storage_bucket: Any, extract_destination_path: str, storage_base_path: str) \
        -> tuple[str, Any, int]:
    """Downloads and extracts index zip from cloud storage.

    Args:
        storage_bucket (google.cloud.storage.bucket.Bucket): google storage bucket where index.zip is stored.
        extract_destination_path (str): the full path of extract folder.
        storage_base_path (str): the source path of the index in the target bucket.
    Returns:
        str: extracted index folder full path.
        Blob: google cloud storage object that represents index.zip blob.
        str: downloaded index generation.

    """
    logging.debug('Start of download_and_extract_index')
    if storage_bucket.name == GCPConfig.PRODUCTION_PRIVATE_BUCKET:
        index_storage_path = os.path.join(GCPConfig.PRIVATE_BASE_PATH, f"{GCPConfig.INDEX_NAME}.zip")
    else:
        index_storage_path = os.path.join(storage_base_path, f"{GCPConfig.INDEX_NAME}.zip")
    download_index_path = os.path.join(extract_destination_path, f"{GCPConfig.INDEX_NAME}.zip")

    index_blob = storage_bucket.blob(index_storage_path)
    index_folder_path = os.path.join(extract_destination_path, GCPConfig.INDEX_NAME)
    index_generation = 0  # Setting to 0 makes the operation succeed only if there are no live versions of the blob

    if not os.path.exists(extract_destination_path):
        Path(extract_destination_path).mkdir()

    if not index_blob.exists():
        Path(index_folder_path).mkdir()
        logging.error(f"{storage_bucket.name} index blob does not exists")
        return index_folder_path, index_blob, index_generation

    index_blob.reload()
    index_generation = index_blob.generation

    index_blob.download_to_filename(download_index_path, if_generation_match=index_generation)

    if os.path.exists(download_index_path):
        with ZipFile(download_index_path, 'r') as index_zip:
            index_zip.extractall(extract_destination_path)

        if not os.path.exists(index_folder_path):
            logging.critical(f"Failed creating {GCPConfig.INDEX_NAME} folder with extracted data.")
            sys.exit(1)

        os.remove(download_index_path)
        logging.success(f"Finished downloading and extracting {GCPConfig.INDEX_NAME} file to "
                        f"{extract_destination_path}")

        return index_folder_path, index_blob, index_generation
    else:
        logging.critical(f"Failed to download {GCPConfig.INDEX_NAME}.zip file from cloud storage.")
        sys.exit(1)


def update_index_folder(index_folder_path: str, pack: Pack, is_private_pack: bool = False,
                        pack_versions_to_keep: list = None) -> bool:
    """
    Updates index folder with pack metadata, changelog and README files.

    Args:
        index_folder_path (str): full path to index folder.
        pack (Pack): a Pack object.
        is_private_pack (bool): Whether the pack is private.
        pack_versions_to_keep (list): pack versions to keep its metadata. If empty, do not remove any versions.

    Returns:
        bool: whether the operation succeeded.
    """
    task_status = False
    index_pack_path = os.path.join(index_folder_path, pack.name)
    pack_versions_to_keep = pack_versions_to_keep or []

    try:
        # skipping index update in case pack is hidden
        if pack.hidden:
            if os.path.exists(index_pack_path):
                shutil.rmtree(index_pack_path)  # remove pack folder inside index in case that it exists
            logging.warning(f"Pack '{pack.name}' is hidden - Skipping updating pack files in index")
            task_status = True
            return task_status

        # Remove old metadata files
        if os.path.exists(index_pack_path):
            for d in os.scandir(index_pack_path):
                if (metadata_version := re.findall(METADATA_FILE_REGEX_GET_VERSION, d.name)) \
                        and pack_versions_to_keep and metadata_version[0] not in pack_versions_to_keep:
                    logging.debug(f"Removing metadata path for pack '{pack.name}': {d.path}")
                    os.remove(d.path)
        else:
            Path(index_pack_path).mkdir()
            logging.debug(f"Created '{pack.name}' pack folder in {GCPConfig.INDEX_NAME}")

        if not pack.is_modified and not is_private_pack:
            logging.debug(f"Updating metadata only with statistics because {pack.name=} {pack.is_modified=}")
            json_write(os.path.join(index_pack_path, "metadata.json"), pack.statistics_metadata, update=True)

            if os.path.exists(os.path.join(index_pack_path, f"metadata-{pack.current_version}.json")):
                json_write(os.path.join(index_pack_path, f"metadata-{pack.current_version}.json"), pack.statistics_metadata,
                           update=True)
            else:
                shutil.copy(os.path.join(index_pack_path, "metadata.json"),
                            os.path.join(index_pack_path, f"metadata-{pack.current_version}.json"))

            task_status = True
            return task_status

        # Copy new files and add metadata for latest version
        for d in os.scandir(pack.path):
            if d.name not in Pack.INDEX_FILES_TO_UPDATE:
                continue

            logging.debug(f"Copying pack's {d.name} file to pack '{pack.name}' index folder")
            shutil.copy(d.path, index_pack_path)
            if pack.current_version and d.name == Pack.METADATA:
                shutil.copy(d.path, os.path.join(index_pack_path, f"metadata-{pack.current_version}.json"))
        logging.debug(f"Finished updating index for pack '{pack.name}'")
        task_status = True
    except Exception as e:
        logging.exception(f"Failed in updating index folder for {pack.name} pack.\n{e}")

    return task_status


def clean_non_existing_packs(index_folder_path: str, private_packs: list, storage_bucket: Any,
                             storage_base_path: str, content_packs: list[Pack], marketplace: str = 'xsoar') -> bool:
    """ Detects packs that are not part of content repo or from private packs bucket.

    In case such packs were detected, problematic pack is deleted from index and from content/packs/{target_pack} path.

    Args:
        index_folder_path (str): full path to downloaded index folder.
        private_packs (list): priced packs from private bucket.
        storage_bucket (google.cloud.storage.bucket.Bucket): google storage bucket where index.zip is stored.
        storage_base_path (str): the source path of the packs in the target bucket.
        pack_list: List[Pack]: The pack list that is created from `create-content-artifacts` step.
        marketplace (str): name of current marketplace the upload is made for. (can be xsoar, marketplacev2 or xpanse)

    Returns:
        bool: whether cleanup was skipped or not.
    """
    logging.debug(f"{GCPConfig.PRODUCTION_BUCKET=}")
    if ('CI' not in os.environ) or (
            os.environ.get('CI_COMMIT_BRANCH') != 'master' and storage_bucket.name == GCPConfig.PRODUCTION_BUCKET) or (
            os.environ.get('CI_COMMIT_BRANCH') == 'master' and storage_bucket.name not in
            (GCPConfig.PRODUCTION_BUCKET, GCPConfig.CI_BUILD_BUCKET)):
        logging.debug("Skipping cleanup of packs in gcs.")  # skipping execution of cleanup in gcs bucket
        return True

    logging.debug("Start cleaning non existing packs in index.")
    valid_pack_names = {p.name for p in content_packs}
    if marketplace in [XSOAR_MP, XSOAR_SAAS_MP]:
        private_packs_names = {p.get('id', '') for p in private_packs}
        valid_pack_names.update(private_packs_names)
    # search for invalid packs folder inside index
    invalid_packs_names = {(entry.name, entry.path) for entry in os.scandir(index_folder_path) if
                           entry.name not in valid_pack_names and entry.is_dir()}
    if invalid_packs_names:
        try:
            logging.warning(f"Found the following invalid packs: {invalid_packs_names}")
            logging.warning(f"Starting cleanup of {len(invalid_packs_names)} invalid packs from gcp and index.zip.")

            for invalid_pack in invalid_packs_names:
                invalid_pack_name = invalid_pack[0]
                invalid_pack_path = invalid_pack[1]
                # remove pack from index
                shutil.rmtree(invalid_pack_path)
                logging.warning(f"Deleted {invalid_pack_name} pack from {GCPConfig.INDEX_NAME} folder")
                # important to add trailing slash at the end of path in order to avoid packs with same prefix
                invalid_pack_gcs_path = os.path.join(storage_base_path, invalid_pack_name, "")  # by design

                for invalid_blob in list(storage_bucket.list_blobs(prefix=invalid_pack_gcs_path)):
                    logging.warning(f"Deleted invalid {invalid_pack_name} pack under url {invalid_blob.public_url}")
                    invalid_blob.delete()  # delete invalid pack in gcs
        except Exception:
            logging.exception("Failed to cleanup non existing packs.")

    else:
        logging.debug(f"No invalid packs detected inside {GCPConfig.INDEX_NAME} folder")

    return False


def prepare_index_json(index_folder_path: str, build_number: str, private_packs: list, commit_hash: str,
                       landing_page_sections: dict = None):
    """
    Prepare and update the index.json file to be uploaded to the bucket.

    Args:
        index_folder_path (str): index folder full path.
        build_number (str): CI build number, used as an index revision.
        private_packs (list): List of private packs and their price.
        commit_hash (str): last commit hash of head.
        landing_page_sections (dict): landingPage sections.

    Returns:
        None
    """
    if not landing_page_sections:
        landing_page_sections = load_json(LANDING_PAGE_SECTIONS_PATH)

    logging.debug(f'commit hash is: {commit_hash}')
    index_json_path = os.path.join(index_folder_path, f'{GCPConfig.INDEX_NAME}.json')
    logging.debug(f'index json path: {index_json_path}')
    logging.debug(f'Private packs are: {private_packs}')
    with open(index_json_path, "w+") as index_file:
        index = {
            'revision': build_number,
            'modified': datetime.utcnow().strftime(Metadata.DATE_FORMAT),
            'packs': private_packs,
            'commit': commit_hash,
            'landingPage': {'sections': landing_page_sections.get('sections', [])}  # type: ignore[union-attr]
        }
        json.dump(index, index_file, indent=4)


def upload_index_to_storage(index_folder_path: str,
                            extract_destination_path: str,
                            index_blob: Any,
                            index_generation: int,
                            is_private: bool = False,
                            artifacts_dir: str | None = None,
                            index_name: str = GCPConfig.INDEX_NAME
                            ):
    """
    Upload updated index zip to cloud storage.

    :param index_folder_path: index folder full path.
    :param extract_destination_path
    :param index_blob: google cloud storage object that represents index.zip blob.
    :param index_generation: downloaded index generation.
    :param is_private: Indicates if upload is private.
    :param artifacts_dir: The CI artifacts directory to upload the index.json to.
    :param index_name: The index name to upload.
    :returns None.

    """
    index_zip_name = os.path.basename(index_folder_path)
    index_zip_path = shutil.make_archive(base_name=index_folder_path, format="zip",
                                         root_dir=extract_destination_path, base_dir=index_zip_name)
    try:
        logging.debug(f'index zip path: {index_zip_path}')

        index_blob.reload()
        current_index_generation = index_blob.generation

        index_blob.cache_control = "no-cache,max-age=0"  # disabling caching for index blob

        if is_private or current_index_generation == index_generation:
            # we upload both index.json and the index.zip to allow usage of index.json without having to unzip
            index_blob.upload_from_filename(index_zip_path)
            logging.success(f"Finished uploading {index_name}.zip to storage.")
        else:
            logging.critical(f"Failed in uploading {index_name}, mismatch in index file generation.")
            logging.critical(f"Downloaded index generation: {index_generation}")
            logging.critical(f"Current index generation: {current_index_generation}")
            sys.exit(0)

    except Exception:
        logging.exception(f"Failed in uploading {index_name}.")
        sys.exit(1)
    finally:
        if artifacts_dir:
            # Store index.json in CircleCI artifacts
            shutil.copyfile(
                os.path.join(index_folder_path, f'{index_name}.json'),
                os.path.join(artifacts_dir, f'{index_name}.json'),
            )
        shutil.rmtree(index_folder_path)


def create_corepacks_config(storage_bucket: Any, build_number: str, index_folder_path: str,
                            artifacts_dir: str, storage_base_path: str, marketplace: str = 'xsoar'):
    """Create corepacks.json file and stores it in the artifacts dir. This file contains all of the server's core
    packs, under the key corepacks, and specifies which core packs should be upgraded upon XSOAR upgrade, under the key
    upgradeCorePacks.


     Args:
        storage_bucket (google.cloud.storage.bucket.Bucket): gcs bucket where core packs config is uploaded.
        build_number (str): circleCI build number.
        index_folder_path (str): The index folder path.
        artifacts_dir (str): The CI artifacts directory to upload the corepacks.json to.
        storage_base_path (str): the source path of the core packs in the target bucket.
        marketplace (str): the marketplace type of the bucket. possible options: xsoar, marketplace_v2 or xpanse

    """
    required_core_packs = GCPConfig.get_core_packs(marketplace)
    corepacks_files_names = [GCPConfig.CORE_PACK_FILE_NAME]
    corepacks_files_names.extend(GCPConfig.get_core_packs_unlocked_files(marketplace))
    logging.debug(f"Updating the following corepacks files: {corepacks_files_names}")
    for corepacks_file in corepacks_files_names:
        logging.debug(f"Creating corepacks file {corepacks_file}.")
        core_packs_public_urls = []
        bucket_core_packs = set()
        packs_missing_metadata = set()
        for pack in os.scandir(index_folder_path):
            if pack.is_dir() and pack.name in required_core_packs:
                pack_metadata_path = os.path.join(index_folder_path, pack.name, Pack.METADATA)

                if not os.path.exists(pack_metadata_path):
                    logging.critical(f"{pack.name} pack {Pack.METADATA} is missing in {GCPConfig.INDEX_NAME}")
                    packs_missing_metadata.add(pack.name)
                    continue

                with open(pack_metadata_path) as metadata_file:
                    metadata = json.load(metadata_file)

                pack_current_version = metadata.get('currentVersion', Pack.PACK_INITIAL_VERSION)
                core_pack_relative_path = os.path.join(pack.name, pack_current_version, f"{pack.name}.zip")
                core_pack_storage_path = os.path.join(storage_base_path, core_pack_relative_path)

                if not storage_bucket.blob(core_pack_storage_path).exists():
                    logging.critical(f"{pack.name} pack does not exist under {core_pack_storage_path} path")
                    sys.exit(1)

                if corepacks_file == GCPConfig.CORE_PACK_FILE_NAME:
                    core_pack_public_url = os.path.join(GCPConfig.GCS_PUBLIC_URL, storage_bucket.name,
                                                        core_pack_storage_path)
                else:  # versioned core pack file
                    core_pack_public_url = core_pack_relative_path  # Use relative paths in versioned core pack files

                core_packs_public_urls.append(core_pack_public_url)
                bucket_core_packs.add(pack.name)

        if packs_missing_metadata:
            logging.critical(f"Missing {Pack.METADATA} in {len(packs_missing_metadata)} packs: "
                             f"{','.join(sorted(packs_missing_metadata))}, exiting...")
            sys.exit(1)

        missing_core_packs = set(required_core_packs).difference(bucket_core_packs)
        unexpected_core_packs = set(bucket_core_packs).difference(required_core_packs)

        if missing_core_packs:
            logging.critical(
                f"Missing {len(missing_core_packs)} packs (expected in core_packs configuration, but not found in bucket): "
                f"{','.join(sorted(missing_core_packs))}, exiting...")
        if unexpected_core_packs:
            logging.critical(
                f"Unexpected {len(missing_core_packs)} packs in bucket (not in the core_packs configuration): "
                f"{','.join(sorted(unexpected_core_packs))}, exiting...")
        if missing_core_packs or unexpected_core_packs:
            sys.exit(1)

        corepacks_json_path = os.path.join(artifacts_dir, corepacks_file)
        core_packs_data = {
            'corePacks': core_packs_public_urls,
            'upgradeCorePacks': GCPConfig.get_core_packs_to_upgrade(marketplace),
            'buildNumber': build_number,
        }
        json_write(corepacks_json_path, core_packs_data)
        logging.success(f"Finished copying {corepacks_file} to artifacts.")


def _build_summary_table(packs_input_list: list, include_pack_status: bool = False) -> Any:
    """Build summary table from pack list

    Args:
        packs_input_list (list): list of Packs
        include_pack_status (bool): whether pack includes status

    Returns:
        PrettyTable: table with upload result of packs.

    """
    table_fields = ["Index", "Pack ID", "Pack Display Name", "Latest Version", "Aggregated Pack Versions"]
    if include_pack_status:
        table_fields.append("Status")
    table = prettytable.PrettyTable()
    table.field_names = table_fields

    for index, pack in enumerate(packs_input_list, start=1):
        pack_status_message = PackStatus[pack.status].value
        row = [index, pack.name, pack.display_name, pack.current_version,
               pack.aggregation_str if pack.aggregated and pack.aggregation_str else "False"]
        if include_pack_status:
            row.append(pack_status_message)
        table.add_row(row)

    return table


def build_summary_table_md(packs_input_list: list, include_pack_status: bool = False) -> str:
    """Build markdown summary table from pack list

    Args:
        packs_input_list (list): list of Packs
        include_pack_status (bool): whether pack includes status

    Returns:
        Markdown table: table with upload result of packs.

    """
    table_fields = ["Index", "Pack ID", "Pack Display Name", "Latest Version", "Status"] if include_pack_status \
        else ["Index", "Pack ID", "Pack Display Name", "Latest Version"]

    table = ['|', '|']

    for key in table_fields:
        table[0] = f'{table[0]} {key} |'
        table[1] = f'{table[1]} :- |'

    for index, pack in enumerate(packs_input_list):
        pack_status_message = PackStatus[pack.status].value if include_pack_status else ''

        row = [index, pack.name, pack.display_name, pack.current_version, pack_status_message] if include_pack_status \
            else [index, pack.name, pack.display_name, pack.current_version]

        row_hr = '|'
        for _value in row:
            row_hr = f'{row_hr} {_value}|'
        table.append(row_hr)

    return '\n'.join(table)


def add_private_content_to_index(private_index_path: str, extract_destination_path: str, index_folder_path: str,
                                 pack_names: set) -> tuple[list | list, list]:
    """ Adds a list of priced packs data-structures to the public index.json file.
    This step should not be skipped even if there are no new or updated private packs.

    Args:
        private_index_path: path to where the private index is located.
        extract_destination_path (str): full path to extract directory.
        index_folder_path (str): downloaded index folder directory path.
        pack_names (set): collection of pack names.

    Returns:
        list: priced packs from private bucket.

    """

    private_packs = []
    updated_private_packs = []

    try:
        private_packs = get_private_packs(private_index_path, pack_names,
                                          extract_destination_path)
        updated_private_packs = get_updated_private_packs(private_packs, index_folder_path)
        add_private_packs_to_index(index_folder_path, private_index_path)

    except Exception as e:
        logging.exception(f"Could not add private packs to the index. Additional Info: {str(e)}")

    finally:
        logging.debug("Finished updating index with priced packs")
        shutil.rmtree(os.path.dirname(private_index_path), ignore_errors=True)
        return private_packs, updated_private_packs


def get_updated_private_packs(private_packs, index_folder_path):
    """ Checks for updated private packs by compering contentCommitHash between public index json and private pack
    metadata files.

    Args:
        private_packs (list): List of dicts containing pack metadata information.
        index_folder_path (str): The public index folder path.

    Returns:
        updated_private_packs (list) : a list of all private packs id's that were updated.

    """
    updated_private_packs = []

    public_index_file_path = os.path.join(index_folder_path, f"{GCPConfig.INDEX_NAME}.json")
    public_index_json = load_json(public_index_file_path)
    private_packs_from_public_index = public_index_json.get("packs", {})

    for pack in private_packs:
        private_pack_id = pack.get('id')
        private_commit_hash_from_metadata = pack.get('contentCommitHash', "")
        private_commit_hash_from_content_repo = ""
        for public_pack in private_packs_from_public_index:
            if public_pack.get('id') == private_pack_id:
                private_commit_hash_from_content_repo = public_pack.get('contentCommitHash', "")

        private_pack_was_updated = private_commit_hash_from_metadata != private_commit_hash_from_content_repo
        if private_pack_was_updated:
            updated_private_packs.append(private_pack_id)

    logging.debug(f"Updated private packs are: {updated_private_packs}")
    return updated_private_packs


def get_private_packs(private_index_path: str, pack_names: set = None,
                      extract_destination_path: str = '') -> list:
    """
    Gets a list of private packs.

    :param private_index_path: Path to where the private index is located.
    :param pack_names: Collection of pack names.
    :param extract_destination_path: Path to where the files should be extracted to.
    :return: List of dicts containing pack metadata information.
    """
    logging.debug(f'getting all private packs. private_index_path: {private_index_path}')
    try:
        metadata_files = glob.glob(f"{private_index_path}/**/metadata.json")
    except Exception:
        logging.exception(f'Could not find metadata files in {private_index_path}.')
        return []

    if not metadata_files:
        logging.warning(f'No metadata files found in [{private_index_path}]')

    private_packs = []
    pack_names = pack_names or set()
    logging.debug(f'all metadata files found: {metadata_files}')
    for metadata_file_path in metadata_files:
        try:
            with open(metadata_file_path) as metadata_file:
                metadata = json.load(metadata_file)
            pack_id = metadata.get('id')
            is_changed_private_pack = pack_id in pack_names
            if is_changed_private_pack:  # Should take metadata from artifacts.
                with open(os.path.join(extract_destination_path, pack_id, "pack_metadata.json")) as metadata_file:
                    metadata = json.load(metadata_file)
            if metadata:
                private_packs.append({
                    'id': metadata.get('id') if not is_changed_private_pack else metadata.get('name'),
                    'price': metadata.get('price'),
                    'vendorId': metadata.get('vendorId', ""),
                    'partnerId': metadata.get('partnerId', ""),
                    'partnerName': metadata.get('partnerName', ""),
                    'disableMonthly': metadata.get('disableMonthly', False),
                    'contentCommitHash': metadata.get('contentCommitHash', "")
                })
        except ValueError:
            logging.exception(f'Invalid JSON in the metadata file [{metadata_file_path}].')

    return private_packs


def add_private_packs_to_index(index_folder_path: str, private_index_path: str):
    """ Add the private packs to the index folder.

    Args:
        index_folder_path: The index folder path.
        private_index_path: The path for the index of the private packs.

    """
    for d in os.scandir(private_index_path):
        if os.path.isdir(d.path):
            pack = Pack(d.name, d.path)
            update_index_folder(index_folder_path, pack, is_private_pack=True)


def is_private_packs_updated(public_index_json, private_index_path):
    """ Checks whether there were changes in private packs from the last upload.
    The check compares the `content commit hash` field in the public index with the value stored in the private index.
    If there is at least one private pack that has been updated/released, the upload should be performed and not
    skipped.

    Args:
        public_index_json (dict) : The public index.json file.
        private_index_path (str): Path to where the private index.zip is located.

    Returns:
        is_private_packs_updated (bool): True if there is at least one private pack that was updated/released,
         False otherwise (i.e there are no private packs that have been updated/released).

    """
    logging.debug("Checking if there are updated private packs")

    private_index_file_path = os.path.join(private_index_path, f"{GCPConfig.INDEX_NAME}.json")
    private_index_json = load_json(private_index_file_path)
    private_packs_from_private_index = private_index_json.get("packs")
    private_packs_from_public_index = public_index_json.get("packs")

    if len(private_packs_from_private_index) != len(private_packs_from_public_index):  # type: ignore[arg-type]
        # private pack was added or deleted
        logging.debug("There is at least one private pack that was added/deleted, upload should not be skipped.")
        return True

    id_to_commit_hash_from_public_index = {private_pack.get("id"): private_pack.get("contentCommitHash", "") for
                                           private_pack in private_packs_from_public_index}

    for private_pack in private_packs_from_private_index:  # type: ignore[union-attr]
        pack_id = private_pack.get("id")
        content_commit_hash = private_pack.get("contentCommitHash", "")
        if id_to_commit_hash_from_public_index.get(pack_id) != content_commit_hash:
            logging.debug("There is at least one private pack that was updated, upload should not be skipped.")
            return True

    logging.debug("No private packs were changed")
    return False


def check_if_index_is_updated(index_folder_path: str, content_repo: Any, current_commit_hash: str,
                              previous_commit_hash: str, storage_bucket: Any,
                              is_private_content_updated: bool = False):
    """ Checks stored at index.json commit hash and compares it to current commit hash. In case no packs folders were
    added/modified/deleted, all other steps are not performed.

    Args:
        index_folder_path (str): index folder full path.
        content_repo (git.repo.base.Repo): content repo object.
        current_commit_hash (str): last commit hash of head.
        previous_commit_hash (str): the previous commit to diff with
        storage_bucket: public storage bucket.
        is_private_content_updated (bool): True if private content updated, False otherwise.

    """
    skipping_build_task_message = "Skipping Upload Packs To Marketplace Storage Step."
    logging.debug(f"{GCPConfig.PRODUCTION_BUCKET=}")

    try:
        if storage_bucket.name not in (GCPConfig.CI_BUILD_BUCKET, GCPConfig.PRODUCTION_BUCKET):
            logging.debug("Skipping index update check in non production/build bucket")
            return

        if is_private_content_updated:
            logging.debug("Skipping index update as Private Content has updated.")
            return

        if not os.path.exists(os.path.join(index_folder_path, f"{GCPConfig.INDEX_NAME}.json")):
            # will happen only in init bucket run
            logging.warning(f"{GCPConfig.INDEX_NAME}.json not found in {GCPConfig.INDEX_NAME} folder")
            return

        with open(os.path.join(index_folder_path, f"{GCPConfig.INDEX_NAME}.json")) as index_file:
            index_json = json.load(index_file)

        index_commit_hash = index_json.get('commit', previous_commit_hash)

        try:
            index_commit = content_repo.commit(index_commit_hash)
        except Exception:
            # not updated build will receive this exception because it is missing more updated commit
            logging.exception(f"Index is already updated. {skipping_build_task_message}")
            sys.exit()

        current_commit = content_repo.commit(current_commit_hash)

        if current_commit.committed_datetime <= index_commit.committed_datetime:
            logging.warning(
                f"Current commit {current_commit.hexsha} committed time: {current_commit.committed_datetime}")
            logging.warning(f"Index commit {index_commit.hexsha} committed time: {index_commit.committed_datetime}")
            logging.warning("Index is already updated.")
            logging.warning(skipping_build_task_message)
            sys.exit()

        for changed_file in current_commit.diff(index_commit):
            if changed_file.a_path.startswith(PACKS_FOLDER):
                logging.debug(
                    f"Found changed packs between index commit {index_commit.hexsha} and {current_commit.hexsha}")
                break
        else:
            logging.warning(f"No changes found between index commit {index_commit.hexsha} and {current_commit.hexsha}")
            logging.warning(skipping_build_task_message)
            sys.exit()
    except Exception:
        logging.exception("Failed in checking status of index")
        sys.exit(1)


def print_packs_summary(successful_packs: list, skipped_packs: list, failed_packs: list,
                        fail_build: bool = True):
    """Prints summary of packs uploaded to gcs.

    Args:
        successful_packs (list): list of packs that were successfully uploaded.
        skipped_packs (list): list of packs that were skipped during upload.
        failed_packs (list): list of packs that were failed during upload.
        fail_build (bool): indicates whether to fail the build upon failing pack to upload or not

    """
    logging.info(
        f"""\n
------------------------------------------ Packs Upload Summary ------------------------------------------
Total number of packs: {len(successful_packs + skipped_packs + failed_packs)}
----------------------------------------------------------------------------------------------------------""")

    if successful_packs:
        successful_packs_table = _build_summary_table(successful_packs)
        logging.success(f"Number of successful uploaded packs: {len(successful_packs)}")
        logging.success(f"Uploaded packs:\n{successful_packs_table}")
        with open('pack_list.txt', 'w') as f:
            f.write(successful_packs_table.get_string())
    if skipped_packs:
        skipped_packs_table = _build_summary_table(skipped_packs, include_pack_status=True)
        logging.warning(f"Number of skipped packs: {len(skipped_packs)}")
        logging.warning(f"Skipped packs:\n{skipped_packs_table}")
    if failed_packs:
        failed_packs_table = _build_summary_table(failed_packs, include_pack_status=True)
        logging.critical(f"Number of failed packs: {len(failed_packs)}")
        logging.critical(f"Failed packs:\n{failed_packs_table}")
        if fail_build:
            # We don't want the bucket upload flow to fail in Prepare Content step if a pack has failed to upload.
            sys.exit(1)

    # for external pull requests -  when there is no failed packs, add the build summary to the pull request
    branch_name = os.environ.get('CI_COMMIT_BRANCH')
    if branch_name and branch_name.startswith('pull/'):
        successful_packs_table = build_summary_table_md(successful_packs)

        build_num = os.environ['CI_BUILD_ID']

        bucket_path = f'https://console.cloud.google.com/storage/browser/' \
                      f'{TEST_XDR_PREFIX}marketplace-ci-build/content/builds/{branch_name}/{build_num}'

        pr_comment = f'Number of successful uploaded packs: {len(successful_packs)}\n' \
                     f'Uploaded packs:\n{successful_packs_table}\n\n' \
                     f'Browse to the build bucket with this address:\n{bucket_path}'

        add_pr_comment(pr_comment)


def add_pr_comment(comment: str):
    """Add comment to the pull request.

    Args:
        comment (string): The comment text.

    """
    token = os.environ['CONTENT_GITHUB_TOKEN']
    branch_name = os.environ['CI_COMMIT_BRANCH']
    sha1 = os.environ['CI_COMMIT_SHA']

    query = f'?q={sha1}+repo:demisto/content+is:pr+is:open+head:{branch_name}+is:open'
    url = 'https://api.github.com/search/issues'
    headers = {'Authorization': 'Bearer ' + token}
    try:
        res = requests.get(url + query, headers=headers, verify=False)
        res_json = handle_github_response(res)
        if res_json and res_json.get('total_count', 0) == 1:
            issue_url = res_json['items'][0].get('comments_url') if res_json.get('items', []) else None
            if issue_url:
                res = requests.post(issue_url, json={'body': comment}, headers=headers, verify=False)
                handle_github_response(res)
        else:
            logging.warning(
                f'Add pull request comment failed: There is more then one open pull request for branch {branch_name}.')
    except Exception:
        logging.exception('Add pull request comment failed.')


def handle_github_response(response: Response) -> dict:
    """
    Handles the response from the GitHub server after making a request.
    :param response: Response from the server.
    :return: The returned response.
    """
    res_dict = response.json()
    if not res_dict.get('ok'):
        logging.warning(f'Add pull request comment failed: {res_dict.get("message")}')
    return res_dict


def get_packs_summary(packs_list):
    """ Returns the packs list divided into 3 lists by their status

    Args:
        packs_list (list): The full packs list

    Returns: 4 lists of packs - successful_packs, successful_uploaded_dependencies_zip_packs, skipped_packs & failed_packs

    """

    successful_packs = []
    successful_uploaded_dependencies_zip_packs = []
    skipped_packs = []
    failed_packs = []
    for pack in packs_list:
        if pack.status == PackStatus.SUCCESS.name:
            successful_packs.append(pack)
        elif pack.status == PackStatus.SUCCESS_CREATING_DEPENDENCIES_ZIP_UPLOADING.name:
            successful_uploaded_dependencies_zip_packs.append(pack)
        elif pack.status in SKIPPED_STATUS_CODES:
            skipped_packs.append(pack)
        else:
            failed_packs.append(pack)

    return successful_packs, successful_uploaded_dependencies_zip_packs, skipped_packs, failed_packs


def handle_private_content(public_index_folder_path, private_bucket_name, extract_destination_path, storage_client,
                           public_pack_names, storage_base_path: str) -> tuple[bool, list, list]:
    """
    1. Add private packs to public index.json.
    2. Checks if there are private packs that were added/deleted/updated.

    Args:
        public_index_folder_path: extracted public index folder full path.
        private_bucket_name: Private storage bucket name
        extract_destination_path: full path to extract directory.
        storage_client : initialized google cloud storage client.
        public_pack_names : unique collection of public packs names to upload.
        storage_base_path (str): the source path in the target bucket.

    Returns:
        is_private_content_updated (bool): True if there is at least one private pack that was updated/released.
        False otherwise (i.e there are no private packs that have been updated/released).
        private_packs (list) : priced packs from private bucket.
        updated_private_packs_ids (list): all private packs id's that were updated.
    """
    if private_bucket_name:
        private_storage_bucket = storage_client.bucket(private_bucket_name)
        private_index_path, _, _ = download_and_extract_index(
            private_storage_bucket, os.path.join(extract_destination_path, "private"), storage_base_path
        )

        public_index_json_file_path = os.path.join(public_index_folder_path, f"{GCPConfig.INDEX_NAME}.json")
        public_index_json = load_json(public_index_json_file_path)

        if public_index_json:
            are_private_packs_updated = is_private_packs_updated(public_index_json, private_index_path)
            private_packs, updated_private_packs_ids = add_private_content_to_index(
                private_index_path, extract_destination_path, public_index_folder_path, public_pack_names
            )
            return are_private_packs_updated, private_packs, updated_private_packs_ids
        else:
            logging.error(f"Public {GCPConfig.INDEX_NAME}.json was found empty.")
            sys.exit(1)
    else:
        return False, [], []


def get_images_data(packs_list: list, markdown_images_dict: dict):
    """ Returns a data structure of all packs that an integration/author image of them was uploaded

    Args:
        packs_list (list): The list of all packs

    Returns:
        The images data structure
    """
    images_data = {}
    pack_image_data: dict = {}

    for pack in packs_list:
        pack_image_data[pack.name] = {}
        if pack.uploaded_author_image:
            pack_image_data[pack.name][BucketUploadFlow.AUTHOR] = True
        if pack.uploaded_integration_images:
            pack_image_data[pack.name][BucketUploadFlow.INTEGRATIONS] = pack.uploaded_integration_images
        if pack.uploaded_preview_images:
            pack_image_data[pack.name][BucketUploadFlow.PREVIEW_IMAGES] = pack.uploaded_preview_images
        if pack.uploaded_dynamic_dashboard_images:
            pack_image_data[pack.name][BucketUploadFlow.DYNAMIC_DASHBOARD_IMAGES] = pack.uploaded_dynamic_dashboard_images
        if pack_image_data[pack.name]:
            images_data.update(pack_image_data)

    images_data[BucketUploadFlow.MARKDOWN_IMAGES] = markdown_images_dict
    return images_data


def upload_packs_with_dependencies_zip(storage_bucket, storage_base_path, signature_key,
                                       packs_dict):
    """
    Uploads packs with mandatory dependencies zip for all packs
    Args:
        signature_key (str): Signature key used for encrypting packs
        storage_base_path (str): The upload destination in the target bucket for all packs (in the format of
                                 <some_path_in_the_target_bucket>/content/Packs).
        storage_bucket (google.cloud.storage.bucket.Bucket): google cloud storage bucket.
        packs_dict (dict): Dict of packs relevant for current marketplace as
        {pack_name: pack_object}

    """
    logging.info("Starting to collect pack with dependencies zips")
    for pack_name, pack in packs_dict.items():
        try:
            if (pack.status not in [*SKIPPED_STATUS_CODES, PackStatus.SUCCESS.name]) or pack.hidden:
                # avoid trying to upload dependencies zip for failed or hidden packs
                continue
            pack_and_its_dependencies = [packs_dict.get(dep_name) for dep_name in
                                         pack.all_levels_dependencies] + [pack]
            pack_or_dependency_was_uploaded = any(dep_pack.status == PackStatus.SUCCESS.name for dep_pack in
                                                  pack_and_its_dependencies)
            if pack_or_dependency_was_uploaded:
                logging.debug(f"Starting to collect pack with dependencies for pack '{pack_name}'. {pack_and_its_dependencies=}")
                pack_with_dep_path = os.path.join(pack.path, "with_dependencies")
                zip_with_deps_path = os.path.join(pack.path, f"{pack_name}_with_dependencies.zip")
                upload_path = os.path.join(storage_base_path, pack_name, f"{pack_name}_with_dependencies.zip")
                Path(pack_with_dep_path).mkdir(parents=True, exist_ok=True)
                for current_pack in pack_and_its_dependencies:
                    if current_pack.hidden:
                        continue
                    logging.debug(f"Starting to collect zip of pack {current_pack.name}")
                    # zip the pack and each of the pack's dependencies (or copy existing zip if was already zipped)
                    if not (current_pack.zip_path and os.path.isfile(current_pack.zip_path)):
                        # the zip does not exist yet, zip the current pack
                        task_status = current_pack.sign_and_zip_pack(signature_key)
                        if not task_status:
                            # modify the pack's status to indicate the failure was in the dependencies zip step
                            pack.status = PackStatus.FAILED_CREATING_DEPENDENCIES_ZIP_SIGNING.name
                            logging.debug(f"Skipping uploading {pack.name} since failed zipping {current_pack.name}.")
                            break
                    shutil.copy(current_pack.zip_path, os.path.join(pack_with_dep_path, current_pack.name + ".zip"))
                if pack.status == PackStatus.FAILED_CREATING_DEPENDENCIES_ZIP_SIGNING.name:
                    break

                logging.debug(f"Zipping {pack_name} with its dependencies")
                Pack.zip_folder_items(pack_with_dep_path, pack_with_dep_path, zip_with_deps_path)
                shutil.rmtree(pack_with_dep_path)
                logging.debug(f"Uploading {pack_name} with its dependencies")
                task_status = pack.upload_to_storage(zip_with_deps_path, storage_bucket, storage_base_path,
                                                     with_dependencies_path=upload_path)
                logging.debug(f"{pack_name} with dependencies was{'' if task_status else ' not'} "
                              f"uploaded successfully")
                if not task_status:
                    pack.status = PackStatus.FAILED_CREATING_DEPENDENCIES_ZIP_UPLOADING.name
                    pack.cleanup()
                else:
                    if pack.status != PackStatus.SUCCESS.name:
                        pack.status = PackStatus.SUCCESS_CREATING_DEPENDENCIES_ZIP_UPLOADING.name
            else:
                logging.debug(f"Pack {pack_name} or its dependency packs were not modified")
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Failed uploading packs with dependencies: {e}")


def delete_from_index_packs_not_in_marketplace(index_folder_path: str,
                                               current_marketplace_packs: list[Pack],
                                               private_packs: list[dict]):
    """
    Delete from index packs that not relevant in the current marketplace from index.
    Args:
        index_folder_path (str): full path to downloaded index folder.
        current_marketplace_packs: List[Pack]: pack list from `create-content-artifacts` step which are filtered by marketplace.
        private_packs: List[dict]: list of private packs info
    Returns:
        set: unique collection of the deleted packs names.
    """
    packs_in_index = set(os.listdir(index_folder_path))
    private_packs_names = {p.get('id', '') for p in private_packs}
    current_marketplace_pack_names = {pack.name for pack in current_marketplace_packs}
    packs_to_be_deleted = packs_in_index - current_marketplace_pack_names - private_packs_names
    deleted_packs = set()
    for pack_name in packs_to_be_deleted:

        try:
            index_pack_path = os.path.join(index_folder_path, pack_name)
            if os.path.exists(os.path.join(index_pack_path, 'metadata.json')):  # verify it's a pack dir
                shutil.rmtree(index_pack_path)  # remove pack folder inside index in case that it exists
                deleted_packs.add(pack_name)
        except Exception:
            logging.error(f'Fail to delete from index the pack {pack_name} which is not in current marketplace')

    logging.debug(f'Packs not supported in current marketplace and was deleted from index: {deleted_packs}')
    return deleted_packs


def should_override_locked_corepacks_file(marketplace: str = 'xsoar'):
    """
    Checks if the corepacks_override.json file in the repo should be used to override an existing corepacks file.
    The override file should be used if the following conditions are met:
    1. The versions-metadata.json file contains a server version that matches the server version specified in the
        override file.
    2. The file version of the server version in the corepacks_override.json file is greater than the matching file
        version in the versions-metadata.json file.
    3. The marketplace to which the upload is taking place matches the marketplace specified in the override file.

    Args
        marketplace (str): the marketplace type of the bucket. possible options: xsoar, marketplace_v2 or xpanse

    Returns True if a file should be updated and False otherwise.
    """
    override_corepacks_server_version = GCPConfig.corepacks_override_contents.get('server_version')
    override_marketplaces = GCPConfig.corepacks_override_contents.get('marketplaces', [])
    override_corepacks_file_version = GCPConfig.corepacks_override_contents.get('file_version')
    current_corepacks_file_version = GCPConfig.core_packs_file_versions.get(
        override_corepacks_server_version, {}).get('file_version')
    if not current_corepacks_file_version:
        logging.debug(f'Could not find a matching file version for server version {override_corepacks_server_version} in '
                      f'{GCPConfig.VERSIONS_METADATA_FILE} file. Skipping upload of {GCPConfig.COREPACKS_OVERRIDE_FILE}...')
        return False

    if int(override_corepacks_file_version) <= int(current_corepacks_file_version):
        logging.debug(
            f'Corepacks file version: {override_corepacks_file_version} of server version {override_corepacks_server_version} in '
            f'{GCPConfig.COREPACKS_OVERRIDE_FILE} is not greater than the version in {GCPConfig.VERSIONS_METADATA_FILE}: '
            f'{current_corepacks_file_version}. Skipping upload of {GCPConfig.COREPACKS_OVERRIDE_FILE}...')
        return False

    if override_marketplaces and marketplace not in override_marketplaces:
        logging.debug(f'Current marketplace {marketplace} is not selected in the {GCPConfig.VERSIONS_METADATA_FILE} '
                      f'file. Skipping upload of {GCPConfig.COREPACKS_OVERRIDE_FILE}...')
        return False

    return True


def override_locked_corepacks_file(build_number: str, artifacts_dir: str):
    """
    Override an existing corepacks-X.X.X.json file, where X.X.X is the server version that was specified in the
    corepacks_override.json file.
    Additionally, update the file version in the versions-metadata.json file, and the corepacks file with the
    current build number.

    Args:
         build_number (str): The build number to use in the corepacks file, if it should be overriden.
         artifacts_dir (str): The CI artifacts directory to upload the corepacks file to.
    """
    # Get the updated content of the corepacks file:
    override_corepacks_server_version = GCPConfig.corepacks_override_contents.get('server_version')
    corepacks_file_new_content = GCPConfig.corepacks_override_contents.get('updated_corepacks_content')

    # Update the build number to the current build number:
    corepacks_file_new_content['buildNumber'] = build_number

    # Upload the updated corepacks file to the given artifacts folder:
    override_corepacks_file_name = f'corepacks-{override_corepacks_server_version}.json'
    logging.debug(f'Overriding {override_corepacks_file_name} with the following content:\n {corepacks_file_new_content}')
    corepacks_json_path = os.path.join(artifacts_dir, override_corepacks_file_name)
    json_write(corepacks_json_path, corepacks_file_new_content)
    logging.success(f"Finished copying overriden {override_corepacks_file_name} to artifacts.")

    # Update the file version of the matching corepacks version in the versions-metadata.json file
    override_corepacks_file_version = GCPConfig.corepacks_override_contents.get('file_version')
    logging.debug(f'Bumping file version of server version {override_corepacks_server_version} in versions-metadata.json from'
                  f'{GCPConfig.versions_metadata_contents["version_map"][override_corepacks_server_version]["file_version"]} to'
                  f'{override_corepacks_file_version}')
    GCPConfig.versions_metadata_contents['version_map'][override_corepacks_server_version]['file_version'] = \
        override_corepacks_file_version


def upload_server_versions_metadata(artifacts_dir: str):
    """
    Upload the versions-metadata.json to the build artifacts folder.

    Args:
        artifacts_dir (str): The CI artifacts directory to upload the versions-metadata.json file to.
    """
    versions_metadata_path = os.path.join(artifacts_dir, GCPConfig.VERSIONS_METADATA_FILE)
    json_write(versions_metadata_path, GCPConfig.versions_metadata_contents)
    logging.success(f"Finished copying {GCPConfig.VERSIONS_METADATA_FILE} to artifacts.")


def option_handler():
    """Validates and parses script arguments.

    Returns:
        Namespace: Parsed arguments object.

    """
    parser = argparse.ArgumentParser(description="Store packs in cloud storage.")
    # disable-secrets-detection-start
    parser.add_argument('-pa', '--packs_artifacts_path', help="The full path of packs artifacts", required=True)
    parser.add_argument('-ast', '--artifacts-folder-server-type', help="The artifacts folder server type",
                        required=True)
    parser.add_argument('-idp', '--id_set_path', help="The full path of id_set.json", required=False)
    parser.add_argument('-e', '--extract_path', help="Full path of folder to extract wanted packs", required=True)
    parser.add_argument('-b', '--bucket_name', help="Storage bucket name", required=True)
    parser.add_argument('-s', '--service_account',
                        help=("Path to gcloud service account, is for circleCI usage. "
                              "For local development use your personal account and "
                              "authenticate using Google Cloud SDK by running: "
                              "`gcloud auth application-default login` and leave this parameter blank. "
                              "For more information go to: "
                              "https://googleapis.dev/python/google-api-core/latest/auth.html"),
                        required=False)
    parser.add_argument('-d', '--pack_dependencies', help="Full path to pack dependencies json file.", required=False)
    parser.add_argument('-pn', '--pack_names',
                        help=("Target packs to upload to gcs."),
                        required=True)
    parser.add_argument('-p', '--upload_specific_pack',
                        type=str2bool, help=("Indication if the -p flag is used and only specific packs are uploded"),
                        default=False)
    parser.add_argument('-n', '--ci_build_number',
                        help="CircleCi build number (will be used as hash revision at index file)", required=False)
    parser.add_argument('-o', '--override_all_packs', help="Override all existing packs in cloud storage",
                        type=str2bool, default=False, required=True)
    parser.add_argument('-k', '--key_string', help="Base64 encoded signature key used for signing packs.",
                        required=False)
    parser.add_argument('-sb', '--storage_base_path', help="Storage base path of the directory to upload to.",
                        required=False)
    parser.add_argument('-rt', '--remove_test_deps', type=str2bool,
                        help='Should remove test playbooks from content packs or not.', default=True)
    parser.add_argument('-bu', '--bucket_upload', help='is bucket upload build?', type=str2bool, required=True)
    parser.add_argument('-pb', '--private_bucket_name', help="Private storage bucket name", required=False)
    parser.add_argument('-c', '--ci_branch', help="CI branch of current build", required=True)
    parser.add_argument('-f', '--force_upload', help="is force upload build?", type=str2bool, required=True)
    parser.add_argument('-dz', '--create_dependencies_zip', type=str2bool, help="Upload packs with dependencies zip",
                        required=False)
    parser.add_argument('-mp', '--marketplace', help="marketplace version", default='xsoar')
    # disable-secrets-detection-end
    return parser.parse_args()


def main():
    install_logging('Prepare_Content_Packs_For_Testing.log', logger=logging)
    option = option_handler()
    packs_artifacts_path = option.packs_artifacts_path
    id_set = None
    try:
        with Neo4jContentGraphInterface():
            pass
    except Exception as e:
        logging.warning(f"Database is not ready, using id_set.json instead.\n{e}")
        id_set = open_id_set_file(option.id_set_path)
    extract_destination_path = option.extract_path
    storage_bucket_name = option.bucket_name
    service_account = option.service_account
    pack_ids_to_upload = get_packs_ids_to_upload(option.pack_names or "")
    upload_specific_pack = option.upload_specific_pack
    build_number = option.ci_build_number if option.ci_build_number else str(uuid.uuid4())
    override_all_packs = option.override_all_packs
    signature_key = option.key_string
    packs_dependencies_mapping = load_json(option.pack_dependencies) if option.pack_dependencies else {}
    storage_base_path = option.storage_base_path
    remove_test_deps = option.remove_test_deps
    is_bucket_upload_flow = option.bucket_upload
    private_bucket_name = option.private_bucket_name
    ci_branch = option.ci_branch
    force_upload = option.force_upload
    marketplace = option.marketplace
    is_create_dependencies_zip = option.create_dependencies_zip

    # regular upload flow that doesn't force or to upload specific packs and not PR or nightly build
    is_regular_upload_flow = is_bucket_upload_flow and not any([force_upload, upload_specific_pack, override_all_packs])

    # google cloud storage client initialized
    storage_client = init_storage_client(service_account)
    storage_bucket = storage_client.bucket(storage_bucket_name)
    uploaded_packs_dir = Path(option.artifacts_folder_server_type) / 'uploaded_packs'
    markdown_images_data = Path(option.artifacts_folder_server_type) / BucketUploadFlow.MARKDOWN_IMAGES_ARTIFACT_FILE_NAME
    uploaded_packs_dir.mkdir(parents=True, exist_ok=True)
    # Relevant when triggering test upload flow
    if storage_bucket_name:
        GCPConfig.PRODUCTION_BUCKET = storage_bucket_name
    logging.debug(f"{GCPConfig.PRODUCTION_BUCKET=}")

    # download and extract index and packs artifacts
    index_folder_path, index_blob, index_generation = download_and_extract_index(storage_bucket,
                                                                                 extract_destination_path,
                                                                                 storage_base_path)
    extract_packs_artifacts(packs_artifacts_path, extract_destination_path)

    # content repo client and current/previous commits initialized
    content_repo = get_content_git_client(CONTENT_ROOT_PATH)
    current_commit_hash, previous_commit_hash = get_recent_commits_data(content_repo, index_folder_path,
                                                                        is_bucket_upload_flow, ci_branch)
    diff_files_list = content_repo.commit(current_commit_hash).diff(content_repo.commit(previous_commit_hash))

    # list of packs to iterate on over and upload/update them in bucket
    all_packs_objects_list = [Pack(pack_id, os.path.join(extract_destination_path, pack_id),
                                   is_modified=pack_id in pack_ids_to_upload)
                              for pack_id in os.listdir(extract_destination_path) if pack_id not in IGNORED_FILES]

    # if it's not a regular upload-flow, then upload only collected/modified packs
    packs_objects_list = all_packs_objects_list if is_regular_upload_flow \
        else [p for p in all_packs_objects_list if p.is_modified]
    logging.info(f"Packs list is: {[p.name for p in packs_objects_list]}")

    # taking care of private packs
    is_private_content_updated, private_packs, updated_private_packs_ids = handle_private_content(
        index_folder_path, private_bucket_name, extract_destination_path, storage_client, pack_ids_to_upload, storage_base_path
    )

    if is_regular_upload_flow:
        check_if_index_is_updated(index_folder_path, content_repo, current_commit_hash, previous_commit_hash,
                                  storage_bucket, is_private_content_updated)

    # clean index and gcs from non existing or invalid packs
    delete_from_index_packs_not_in_marketplace(index_folder_path, all_packs_objects_list, private_packs)
    clean_non_existing_packs(index_folder_path, private_packs, storage_bucket, storage_base_path, all_packs_objects_list,
                             marketplace)

    # initiate the statistics handler for marketplace packs
    statistics_handler = StatisticsHandler(service_account, index_folder_path)

    # iterating over packs that are for the current marketplace
    for pack in packs_objects_list:
        logging.debug(f"Starts iterating over pack '{pack.name}' which is{' not ' if not pack.is_modified else ' '}modified")

        if not pack.load_pack_metadata():
            pack.status = PackStatus.FAILED_LOADING_PACK_METADATA.value  # type: ignore[misc]
            pack.cleanup()
            continue

        if not pack.enhance_pack_attributes(index_folder_path, packs_dependencies_mapping, marketplace,
                                            statistics_handler, remove_test_deps):
            pack.status = PackStatus.FAILED_ENHANCING_PACK_ATTRIBUTES.value  # type: ignore[misc]
            pack.cleanup()
            continue

        task_status, not_updated_build, pack_versions_to_keep = pack.prepare_release_notes(
            index_folder_path,
            build_number,
            diff_files_list,
            marketplace, id_set,
            is_override=override_all_packs
        )

        if not task_status:
            pack.status = PackStatus.FAILED_RELEASE_NOTES.name  # type: ignore[misc]
            pack.cleanup()
            continue

        if not_updated_build:
            pack.status = PackStatus.PACK_IS_NOT_UPDATED_IN_RUNNING_BUILD.name  # type: ignore[misc]
            continue

        if pack.is_modified:
            if not pack.format_metadata(remove_test_deps=remove_test_deps):
                pack.status = PackStatus.FAILED_METADATA_PARSING.name  # type: ignore[misc]
                pack.cleanup()
                continue

            # upload author integration images and readme images
            if not pack.upload_images(storage_bucket, storage_base_path, marketplace):
                continue

            if not pack.sign_and_zip_pack(signature_key, uploaded_packs_dir):
                continue

            if not pack.upload_to_storage(pack.zip_path, storage_bucket, storage_base_path):
                pack.status = PackStatus.FAILED_UPLOADING_PACK.name  # type: ignore[misc]
                pack.cleanup()
                continue
        else:
            # Signs and zips non-modified packs for the upload_with_dependencies phase
            if not pack.sign_and_zip_pack(signature_key, uploaded_packs_dir):
                continue

        if not update_index_folder(index_folder_path=index_folder_path, pack=pack, pack_versions_to_keep=pack_versions_to_keep):
            pack.status = PackStatus.FAILED_UPDATING_INDEX_FOLDER.name  # type: ignore[misc]
            pack.cleanup()
            continue

        logging.debug(f"Finished iterating over pack '{pack.name}'")
        if not pack.is_modified:
            pack.status = PackStatus.PACK_ALREADY_EXISTS.name  # type: ignore[misc]
            pack.cleanup()
            continue

        pack.status = PackStatus.SUCCESS.name  # type: ignore[misc]

    # upload core packs json to bucket
    create_corepacks_config(storage_bucket, build_number, index_folder_path,
                            os.path.dirname(packs_artifacts_path), storage_base_path, marketplace)

    # override a locked core packs file (used for hot-fixes)
    if should_override_locked_corepacks_file(marketplace=marketplace):
        logging.debug('Using the corepacks_override.json file to update an existing corepacks file.')
        override_locked_corepacks_file(build_number=build_number, artifacts_dir=os.path.dirname(packs_artifacts_path))
    else:
        logging.debug('Skipping overriding an existing corepacks file.')

    # upload server versions metadata to bucket
    upload_server_versions_metadata(os.path.dirname(packs_artifacts_path))

    prepare_index_json(index_folder_path=index_folder_path,
                       build_number=build_number,
                       private_packs=private_packs,
                       commit_hash=current_commit_hash if is_regular_upload_flow or override_all_packs else previous_commit_hash,
                       landing_page_sections=statistics_handler.landing_page_sections)

    # finished iteration over content packs
    upload_index_to_storage(index_folder_path=index_folder_path,
                            extract_destination_path=extract_destination_path,
                            index_blob=index_blob,
                            index_generation=index_generation,
                            artifacts_dir=os.path.dirname(packs_artifacts_path)
                            )

    # dependencies zip is currently supported only for marketplace=xsoar, not for xsiam/xpanse
    if is_create_dependencies_zip and marketplace == XSOAR_MP:
        # handle packs with dependencies zip
        all_packs_dict = {p.name: p for p in all_packs_objects_list}
        upload_packs_with_dependencies_zip(storage_bucket, storage_base_path, signature_key,
                                           all_packs_dict)

    markdown_images_dict = download_markdown_images_from_artifacts(
        markdown_images_data, storage_bucket=storage_bucket, storge_base_path=storage_base_path)

    # get the lists of packs divided by their status
    successful_packs, successful_uploaded_dependencies_zip_packs, skipped_packs, failed_packs = get_packs_summary(
        packs_objects_list)

    # Store successful and failed packs list in CircleCI artifacts - to be used in Upload Packs To Marketplace job
    packs_results_file_path = os.path.join(os.path.dirname(packs_artifacts_path), BucketUploadFlow.PACKS_RESULTS_FILE)
    store_successful_and_failed_packs_in_ci_artifacts(
        packs_results_file_path, BucketUploadFlow.PREPARE_CONTENT_FOR_TESTING, successful_packs,
        successful_uploaded_dependencies_zip_packs, failed_packs, updated_private_packs_ids,
        images_data=get_images_data(packs_objects_list, markdown_images_dict=markdown_images_dict)
    )

    # summary of packs status
    print_packs_summary(successful_packs, skipped_packs, failed_packs, not is_bucket_upload_flow)


if __name__ == '__main__':
    main()
