import json
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import draymed
import httpx
from faker import Faker
from flask_batteries_included.helpers import generate_uuid
from flask_batteries_included.helpers.error_handler import ServiceUnavailableException
from flask_batteries_included.helpers.timestamp import (
    parse_iso8601_to_date,
    parse_iso8601_to_datetime,
)
from she_logging import logger
from she_logging.request_id import current_request_id

from dhos_janitor_api.blueprint_api.client import (
    ClientRepository,
    activation_auth_client,
    encounters_client,
    fuego_client,
    gdm_bff_client,
    locations_client,
    messages_client,
    questions_client,
    send_bff_client,
    services_client,
    telemetry_client,
    trustomer_client,
    users_client,
)
from dhos_janitor_api.blueprint_api.controller import (
    auth_controller,
    generator_controller,
)
from dhos_janitor_api.blueprint_api.generator.encounter_generator import (
    EncountersGenerator,
)
from dhos_janitor_api.blueprint_api.generator.message_generator import MessageGenerator
from dhos_janitor_api.blueprint_api.generator.observations_generator import (
    ObservationsGenerator,
    Trajectory,
)
from dhos_janitor_api.blueprint_api.generator.readings_generator import (
    ReadingsGenerator,
)
from dhos_janitor_api.blueprint_api.janitor_thread import JanitorThread
from dhos_janitor_api.config import resettable_targets
from dhos_janitor_api.helpers.handlers import catch_and_log_deprecated_route

GENERATED_CLINICIAN_PASSWORD = "Pass@word1!"
WEIGHTED_RANDOM = (
    list(range(1, 50))
    + [1] * 10
    + [2] * 10
    + [3] * 10
    + [4] * 10
    + [5] * 10
    + [6] * 10
    + [7] * 10
    + [8] * 10
    + [9] * 10
)

DAYS_BETWEEN_SPO2_SCALE_CHANGE = 14
WARD_SCT_CODE = draymed.codes.code_from_name("ward", category="location")
HOSPITAL_SCT_CODE = draymed.codes.code_from_name("hospital", category="location")
BAY_SCT_CODE = draymed.codes.code_from_name("bay", category="location")
BED_SCT_CODE = draymed.codes.code_from_name("bed", category="location")

fake = Faker(locale="en_GB")


class DateHelper:
    """A class that formats a date with a given offset from today.
    Use in a formatted string, e.g.
       "{today:+5}".format(today=DateHelper())
       outputs a date 5 days in the future.
    """

    def __format__(self, offset: str) -> str:
        if not offset:
            offset = "0"

        d = datetime.utcnow().date() + timedelta(days=int(offset))
        return d.strftime("%Y-%m-%d")


def start_reset_thread(
    reset_details: Dict,
    product_settings: Dict[str, Dict[str, Any]],
    num_hospitals: Optional[int] = None,
    num_wards: Optional[int] = None,
) -> str:
    task_uuid: str = generate_uuid()

    location_config: Optional[Dict] = None
    if num_hospitals and num_wards:
        location_config = {"hospitals": num_hospitals, "wards": num_wards}

    thread = JanitorThread(
        task_uuid=task_uuid,
        target=reset_microservices,
        request_id=current_request_id(),
        require_context=True,
    )
    thread.start(
        reset_request=reset_details,
        product_settings=product_settings,
        location_config=location_config,
    )
    return task_uuid


def reset_microservices(
    clients: ClientRepository,
    reset_request: Dict,
    product_settings: Dict[str, Dict[str, Any]],
    location_config: Optional[Dict] = None,
) -> Dict:
    requested_targets: Set[str] = {
        t.replace("-", "_") for t in reset_request.get("targets", [])
    }

    if not requested_targets:
        logger.debug("No microservices specified, defaulting to reset all")

    response_targets = {}
    trustomer_config: Dict = trustomer_client.get_trustomer_config(clients=clients)
    targets = tuple(
        resettable_targets(targets=requested_targets, trustomer_config=trustomer_config)
    )

    for drop_target in targets:
        logger.info("Dropping target %s", drop_target)
        response_targets[drop_target.replace("_", "-")] = drop_service(
            clients=clients,
            target=drop_target,
        )

    for reset_target in targets:
        logger.info("Resetting target %s", reset_target)
        populate_service(
            clients=clients,
            target=reset_target,
            product_settings=product_settings,
            location_config=location_config,
        )

    return response_targets


def drop_service(clients: ClientRepository, target: str) -> Dict:
    logger.debug("Dropping data for target %s", target)
    client: httpx.Client = getattr(clients, target)
    try:
        target_response = client.post(
            "/drop_data",
            headers={"Authorization": f"Bearer {auth_controller.get_system_jwt()}"},
            timeout=30,
        )
        target_response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.debug("Failed to drop data in target %s", target)
        raise ServiceUnavailableException(e)
    except Exception as e:
        logger.debug("Exception dropping data in target %s", target)
        raise ServiceUnavailableException(e)
    catch_and_log_deprecated_route(target_response)
    target_response_json = target_response.json()
    logger.debug("Dropped data for target %s", target)
    return target_response_json


def populate_service(
    clients: ClientRepository,
    target: str,
    product_settings: Dict[str, Dict[str, Any]],
    location_config: Optional[Dict] = None,
) -> None:
    if target == "dhos_services_api":
        populate_dhos_services(clients=clients, product_settings=product_settings)
    elif target == "dhos_users_api":
        populate_dhos_users(clients=clients)
    elif target == "dhos_locations_api":
        populate_dhos_locations(clients=clients, location_config=location_config)
    elif target == "dhos_activation_auth_api":
        populate_dhos_activation_auth(clients=clients)
    elif target == "dhos_messages_api":
        populate_dhos_messages(clients=clients)
    elif target == "gdm_bg_readings_api":
        populate_gdm_bg_readings(clients=clients, product_settings=product_settings)
    elif target == "dhos_questions_api":
        populate_dhos_questions(clients=clients)
    elif target == "dhos_telemetry_api":
        populate_dhos_telemetry(clients=clients)
    elif target == "dhos_encounters_api":  # Data dropped through dhos-services
        populate_dhos_encounters(clients=clients)
    elif target == "dhos_observations_api":
        populate_dhos_observations(clients=clients)
    elif target == "dhos_fuego_api":
        populate_dhos_fuego(clients=clients)
    elif target == "dhos_audit_api":
        logger.critical("No populate to perform for target %s", target)
    else:
        logger.critical("No populate to perform for target %s", target)
        raise ValueError(f"No populate to perform for target {target}")


def populate_dhos_users(
    clients: ClientRepository,
) -> None:
    system_jwt = auth_controller.get_system_jwt()
    dhos_services_data = json.loads(
        (
            Path.cwd() / "dhos_janitor_api" / "data" / "dhos_services_data.json"
        ).read_text()
    )
    clinicians = dhos_services_data.pop("clinician", [])
    # CLINICIANS
    logger.debug("Posting clinicians")
    for clinician in clinicians:
        logger.debug(
            "Posting clinician %s with email %s",
            clinician["uuid"],
            clinician["email_address"],
        )
        # In the JSON we have stored the expiry date as an offset, so replace with a real date
        # relative to the current date.
        exp = clinician.get("contract_expiry_eod_date")
        if exp is not None:
            clinician["contract_expiry_eod_date"] = exp.format(today=DateHelper())

        users_client.create_clinician(
            clients=clients,
            clinician_details=clinician,
            system_jwt=system_jwt,
        )
        users_client.update_clinician(
            clients=clients,
            clinician_email=clinician["email_address"],
            clinician_details={"password": GENERATED_CLINICIAN_PASSWORD},
            system_jwt=system_jwt,
        )


def populate_dhos_services(
    clients: ClientRepository,
    product_settings: Dict[str, Dict[str, Any]],
) -> None:
    dhos_services_data = json.loads(
        (
            Path.cwd() / "dhos_janitor_api" / "data" / "dhos_services_data.json"
        ).read_text()
    )
    clinicians = dhos_services_data.pop("clinician", [])
    # PATIENTS
    logger.debug("Posting patients")
    # GDM patients are posted by clinicians;
    # SEND patients are posted by the system;
    # (product_code, list of patients, Set of allowed_roles)
    product_patients: Tuple = (
        (
            "GDM",
            _open_and_closed_patients(
                clients, product_settings["GDM"]["number_of_patients"], "GDM"
            ),
            {"GDM Superclinician"},
        ),
        (
            "DBM",
            _open_and_closed_patients(
                clients, product_settings["DBM"]["number_of_patients"], "DBM"
            ),
            {"DBM Clinician", "DBM Superclinician"},
        ),
        (
            "SEND",
            [
                generator_controller.generate_patient(clients, "SEND")
                for _ in range(product_settings["SEND"]["number_of_patients"])
            ],
            {"SEND Clinician", "SEND Superclinician"},
        ),
    )
    logger.debug("Posting generated patients")
    for product_code, patients, allowed_roles in product_patients:
        clinician_jwt = get_random_clinician_jwt(clinicians, allowed_roles)
        for patient in patients:
            services_client.create_patient(
                clients=clients,
                patient_details=patient,
                product_name=product_code,
                clinician_jwt=clinician_jwt,
            )


def populate_dhos_locations(
    clients: ClientRepository,
    location_config: Optional[Dict] = None,
) -> None:
    system_jwt = auth_controller.get_system_jwt()
    dhos_locations_data = json.loads(
        (
            Path.cwd() / "dhos_janitor_api" / "data" / "dhos_locations_data.json"
        ).read_text()
    )

    logger.debug("Posting locations")
    locations = dhos_locations_data.pop("location", list())
    if location_config:
        # POST all the GDM and DBM Locations from dhos_services_data because clinicians are likely to rely on these
        logger.debug("Generating locations from location_config")
        for location in locations:
            product_names = [p["product_name"] for p in location["dh_products"]]
            if "GDM" in product_names or "DBM" in product_names:
                locations_client.create_location(
                    clients=clients,
                    location=location,
                    system_jwt=system_jwt,
                )

        hospitals: List[Dict] = []
        for _ in range(location_config["hospitals"]):
            hospital = make_location()
            locations_client.create_location(
                clients=clients,
                location=hospital,
                system_jwt=system_jwt,
            )
            hospitals.append(hospital)

        wards: List[Dict] = []
        for i in range(location_config["wards"]):
            ward = make_location(
                location_type=WARD_SCT_CODE,
                parent=random.choice(hospitals),
                suffix=str(i + 1),
            )
            locations_client.create_location(
                clients=clients,
                location=ward,
                system_jwt=system_jwt,
            )
            wards.append(ward)

        wards_with_bays = []
        bays: List[Dict] = []
        for ward in wards:
            if random.choice((True, False)):
                continue

            wards_with_bays.append(ward)
            for i in range(3):
                bay = make_location(
                    location_type=BAY_SCT_CODE, parent=ward, suffix=str(i + 1)
                )
                locations_client.create_location(
                    clients=clients,
                    location=bay,
                    system_jwt=system_jwt,
                )
                bays.append(bay)

        wards_with_beds = []
        bays_with_beds = []
        beds: List[Dict] = []
        for bay_or_ward in [w for w in wards if w not in wards_with_bays] + bays:
            if random.choice((True, False)):
                continue

            if bay_or_ward in wards:
                wards_with_beds.append(bay_or_ward)
            else:
                bays_with_beds.append(bay_or_ward)

            for i in range(3):
                bed = make_location(
                    location_type=BED_SCT_CODE, parent=bay_or_ward, suffix=str(i + 1)
                )
                locations_client.create_location(
                    clients=clients,
                    location=bed,
                    system_jwt=system_jwt,
                )
                beds.append(bed)

        n_hospitals = len(hospitals)
        n_wards = len(wards)
        n_bays = len(bays)
        n_beds = len(beds)

        logger.info(
            "Generated %d locations.",
            n_hospitals + n_wards + n_bays + n_beds,
            extra={
                "hospitals": n_hospitals,
                "wards": n_wards,
                "bays": n_bays,
                "beds": n_beds,
                "wards with bays but without beds": len([wards_with_bays]),
                "wards with beds but without bays": len(wards_with_beds),
                "bays with beds": len(bays_with_beds),
            },
        )

    else:
        for location in locations:
            locations_client.create_location(
                clients=clients,
                location=location,
                system_jwt=system_jwt,
            )


def get_random_clinician(clinicians: List[Dict], allowed_roles: Set[str]) -> Dict:
    return random.choice(
        [
            clinician
            for clinician in clinicians
            if set(clinician["groups"]).intersection(allowed_roles)
            and clinician.get("contract_expiry_eod_date") is None
        ]
    )


def get_random_clinician_jwt(
    clinicians: List[Dict], allowed_roles: Set[str], use_system_jwt: bool = False
) -> str:
    if use_system_jwt:
        logger.debug("Using system for patient generation")
        return auth_controller.get_system_jwt()

    clinician = get_random_clinician(clinicians, allowed_roles)
    logger.debug("Using clinician for patient generation: %s", clinician["uuid"])
    return auth_controller.get_clinician_jwt(
        clinician["email_address"],
        GENERATED_CLINICIAN_PASSWORD,
        clinician_uuid=clinician["uuid"],
    )


def populate_dhos_activation_auth(clients: ClientRepository) -> None:
    # Only reset GDM activations for static patients.
    system_jwt: str = auth_controller.get_system_jwt()

    for patient_uuid in (f"static_patient_uuid_{i}" for i in range(1, 10)):
        logger.debug("Posting activation for GDM patient with UUID %s", patient_uuid)
        activation_auth_client.create_activation_for_patient(
            clients=clients,
            patient_id=patient_uuid,
            system_jwt=system_jwt,
        )

    # Create static SEND devices.
    send_location_ids = list(
        locations_client.get_all_locations(
            clients=clients,
            product_name="SEND",
            location_types=["225746001"],
            system_jwt=system_jwt,
        )
    )

    for device_uuid in (f"static_device_uuid_D{i}" for i in range(1, 10)):
        logger.debug("Posting activation for SEND device with UUID %s", device_uuid)
        activation_auth_client.create_device(
            clients=clients,
            device_id=device_uuid,
            location_id=random.choice(send_location_ids),
            system_jwt=system_jwt,
        )
        activation_auth_client.create_activation_for_device(
            clients=clients,
            device_id=device_uuid,
            system_jwt=system_jwt,
        )


def get_location_uuids_for_products(
    clients: ClientRepository, product_names: List[str]
) -> Set[str]:
    system_jwt = auth_controller.get_system_jwt()
    locations = locations_client.get_all_locations(
        clients=clients,
        product_name=product_names,
        system_jwt=system_jwt,
    )
    return set(locations.keys())


def get_patients_for_locations_and_products(
    clients: ClientRepository, product_names: List[str], location_uuids: Set[str]
) -> List[Dict]:
    system_jwt = auth_controller.get_system_jwt()
    patient_uuids: Set[str] = set()
    patients: List[Dict] = []
    for product_name in product_names:
        for location_uuid in location_uuids:
            for patient in services_client.get_patients_at_location(
                clients=clients,
                location_uuid=location_uuid,
                product_name=product_name,
                system_jwt=system_jwt,
            ):
                if patient["uuid"] not in patient_uuids:
                    patients.append(patient)
                patient_uuids.add(patient["uuid"])

    return patients


def populate_gdm_bg_readings(clients: ClientRepository, product_settings: Dict) -> None:
    product_names: List[str] = [p for p in {"GDM", "DBM"} if p in product_settings]
    location_uuids = get_location_uuids_for_products(clients, product_names)
    patients = get_patients_for_locations_and_products(
        clients, product_names, location_uuids
    )

    for patient in patients:
        logger.debug("Creating patient readings for patient %s", patient["uuid"])
        readings: List[Dict] = ReadingsGenerator(patient=patient).generate_data()
        logger.debug("Generated %d readings", len(readings))
        patient_jwt: str = auth_controller.get_patient_jwt(
            clients=clients, patient_id=patient["uuid"]
        )

        for i, reading in enumerate(readings):
            logger.debug("Posting reading %d/%d", i + 1, len(readings))
            gdm_bff_client.create_reading(
                clients=clients,
                reading_details=reading,
                patient_id=patient["uuid"],
                patient_jwt=patient_jwt,
            )


def populate_dhos_messages(clients: ClientRepository) -> None:
    system_jwt = auth_controller.get_system_jwt()
    locations = locations_client.get_all_locations(
        clients=clients,
        product_name="GDM",
        system_jwt=system_jwt,
    )

    for location_uuid, location in locations.items():
        logger.debug("Getting patients at location: %s", location["display_name"])
        patients = services_client.get_patients_at_location(
            clients=clients,
            location_uuid=location_uuid,
            product_name="GDM",
            system_jwt=system_jwt,
        )
        for patient in patients:
            logger.debug("Creating patient messages")
            clinicians = users_client.get_clinicians_at_location(
                clients=clients,
                location_uuid=location_uuid,
                system_jwt=system_jwt,
            )
            clinicians = [
                c
                for c in clinicians
                if "gdm" in [p["product_name"].lower() for p in c["products"]]
            ]

            # Random number of messages between 0 and equivalent of one per week
            date_start = parse_iso8601_to_date(patient["dh_products"][0]["opened_date"])
            if date_start is None:
                raise ValueError("No opened date for product")
            date_difference = date.today() - date_start

            clinician_random = get_random_clinician(
                clinicians, {"GDM Clinician", "GDM Superclinician"}
            )
            messages = MessageGenerator(patient=patient).generate_message_data(
                number_of_messages=random.randint(0, max(0, date_difference.days // 7))
            )
            for message in messages:
                # Generate a JWT depending on the message sender.
                if message["sender_type"] == "system":
                    jwt = auth_controller.get_system_jwt("dhos-robot")
                    headers = {}
                elif message["sender_type"] == "location":
                    jwt = auth_controller.get_clinician_jwt(
                        clinician_random["email_address"],
                        GENERATED_CLINICIAN_PASSWORD,
                        clinician_uuid=clinician_random["uuid"],
                    )
                    headers = {
                        "X-Location-Ids": ",".join(clinician_random["locations"])
                    }
                elif message["sender_type"] == "patient":
                    jwt = auth_controller.get_patient_jwt(
                        clients=clients,
                        patient_id=patient["uuid"],
                    )
                    headers = {}
                else:
                    raise ValueError(
                        f"Unexpected message sender type '{message['sender_type']}'"
                    )

                messages_client.create_message(
                    clients=clients,
                    message=message,
                    jwt=jwt,
                    headers=headers,
                )


def populate_dhos_questions(clients: ClientRepository) -> None:
    system_jwt = auth_controller.get_system_jwt()
    json_file = Path.cwd() / "dhos_janitor_api" / "data" / "dhos_questions_data.json"
    with json_file.open(encoding="utf-8") as f:
        questions_data = json.loads(f.read())

    # QUESTION TYPES
    logger.debug("Posting question types")
    question_types = questions_data.pop("question_type", list())
    for question_type in question_types:
        logger.debug("Posting question_type with UUID: %s", question_type["uuid"])
        questions_client.create_question_type(
            clients=clients,
            question_type=question_type,
            system_jwt=system_jwt,
        )

    # QUESTION OPTION TYPES
    logger.debug("Posting question option types")
    question_option_types = questions_data.pop("question_option_type", list())
    for question_option_type in question_option_types:
        logger.debug(
            "Posting question_option_type with UUID %s", question_option_type["uuid"]
        )
        questions_client.create_question_option_type(
            clients=clients,
            question_option_type=question_option_type,
            system_jwt=system_jwt,
        )

    # QUESTIONS
    logger.debug("Posting questions")
    questions = questions_data.pop("question", list())
    for question in questions:
        logger.debug("Posting question '%s'", question["question"])
        questions_client.create_question(
            clients=clients,
            question=question,
            system_jwt=system_jwt,
        )


def populate_dhos_telemetry(clients: ClientRepository) -> None:
    # collect all patients in trust locations
    system_jwt = auth_controller.get_system_jwt()
    patients: Dict = {}
    clinicians: Dict = {}
    locations = locations_client.get_all_locations(
        clients=clients,
        product_name="GDM",
        system_jwt=system_jwt,
    )
    for location_uuid, location in locations.items():
        logger.info("Getting patients at location: %s", location["display_name"])
        for patient in services_client.get_patients_at_location(
            clients=clients,
            location_uuid=location_uuid,
            product_name="GDM",
            system_jwt=system_jwt,
        ):
            patients[patient["uuid"]] = patient

        logger.info("Getting clinicians at location: %s", location["display_name"])
        for clinician in (
            c
            for c in users_client.get_clinicians_at_location(
                clients=clients,
                location_uuid=location_uuid,
                system_jwt=system_jwt,
            )
            if "gdm" in [p["product_name"].lower() for p in c["products"]]
        ):
            clinicians[clinician["uuid"]] = clinician

    json_file = Path.cwd() / "dhos_janitor_api" / "data" / "dhos_telemetry_data.json"
    with json_file.open(encoding="utf-8") as f:
        telemetry_data = json.loads(f.read())

    for installation in telemetry_data["mobile"]:
        random_patient: Dict = random.choice(list(patients.values()))
        logger.debug(
            "Posting mobile installation for patient %s", random_patient["uuid"]
        )
        telemetry_client.create_patient_installation(
            clients=clients,
            patient_id=random_patient["uuid"],
            installation=installation,
            patient_jwt=auth_controller.get_patient_jwt(
                clients=clients,
                patient_id=random_patient["uuid"],
            ),
        )

    for installation in telemetry_data["desktop"]:
        random_clinician: Dict = random.choice(list(clinicians.values()))
        logger.debug(
            "Posting desktop installation for clinician %s", random_clinician["uuid"]
        )
        telemetry_client.create_clinician_installation(
            clients=clients,
            clinician_id=random_clinician["uuid"],
            installation=installation,
            clinician_jwt=auth_controller.get_clinician_jwt(
                random_clinician["email_address"],
                GENERATED_CLINICIAN_PASSWORD,
                clinician_uuid=random_clinician["uuid"],
            ),
        )


def populate_dhos_encounters(clients: ClientRepository) -> None:
    def _discharge_date_for_spo2_history_change(encounter: Dict) -> datetime:
        if encounter["discharged_at"] is None:
            return datetime.utcnow().replace(tzinfo=timezone.utc)
        discharge_date = parse_iso8601_to_datetime(encounter["discharged_at"])
        if discharge_date is None:
            raise ValueError("Couldn't convert discharge date")
        return discharge_date

    system_jwt = auth_controller.get_system_jwt()
    patients = services_client.search_patients(
        clients=clients,
        system_jwt=system_jwt,
        product_name="SEND",
        active=True,
    )
    generator = EncountersGenerator(
        clients=clients,
        system_jwt=system_jwt,
        ward_sct_code=WARD_SCT_CODE,
        bay_sct_code=BAY_SCT_CODE,
        bed_sct_code=BED_SCT_CODE,
    )
    clinician_jwt = _get_stan_lee_jwt()

    for patient in patients:
        num_encounters: int = random.randint(0, 5)

        for i in range(num_encounters):
            discharged: bool = i < (num_encounters - 1)
            encounter = generator.generate_data_for_patient(patient, discharged)
            encounter = encounters_client.create_encounter(
                clients=clients,
                encounter=encounter,
                clinician_jwt=clinician_jwt,
            )
            encounter_id: str = encounter["uuid"]
            if random.random() > 0.7:
                discharged_date: datetime = _discharge_date_for_spo2_history_change(
                    encounter
                )
                admitted_at_iso8601 = parse_iso8601_to_datetime(
                    encounter["admitted_at"]
                )
                if admitted_at_iso8601 is None:
                    raise ValueError("No admission datetime")
                days_in_hospital: timedelta = discharged_date - admitted_at_iso8601
                number_of_scale_changes: int = math.floor(
                    days_in_hospital.days / DAYS_BETWEEN_SPO2_SCALE_CHANGE
                )
                minimum_spo2_scale_date: datetime = admitted_at_iso8601
                for scale_calculated_index in range(number_of_scale_changes):
                    day_gap_between_scale_changes: float = (
                        DAYS_BETWEEN_SPO2_SCALE_CHANGE
                    )
                    if number_of_scale_changes == 1:
                        day_gap_between_scale_changes = days_in_hospital.days / 2
                    minimum_spo2_scale_date = minimum_spo2_scale_date + timedelta(
                        days=day_gap_between_scale_changes
                    )
                    spo2_scale: int = 2 if scale_calculated_index % 2 == 0 else 1
                    spo2_history = encounters_client.update_spo2_scale(
                        clients=clients,
                        encounter_id=encounter_id,
                        spo2_scale=spo2_scale,
                        clinician_jwt=clinician_jwt,
                    )
                    minimum_spo2_scale_date = encounters_client.update_spo2_history(
                        clients=clients,
                        score_system_history_id=spo2_history["uuid"],
                        spo2_scale_time=minimum_spo2_scale_date,
                        system_jwt=system_jwt,
                    )


def _get_stan_lee_jwt() -> str:
    return auth_controller.get_clinician_jwt(
        "stan.lee@mail.com",
        GENERATED_CLINICIAN_PASSWORD,
        clinician_uuid="static_clinician_uuid_G",
    )


def populate_dhos_observations(clients: ClientRepository) -> None:
    logger.debug("Getting SEND locations")
    system_jwt = auth_controller.get_system_jwt()
    locations = locations_client.get_all_locations(
        clients=clients,
        product_name="SEND",
        system_jwt=system_jwt,
        location_types=["225746001"],
    )
    logger.debug("Got %d locations", len(locations))
    i_loc = 0
    for location_uuid, location in locations.items():
        encounters: List[Dict] = send_bff_client.search_encounters(
            clients=clients, location_uuid=location_uuid, system_jwt=system_jwt
        )["results"]
        logger.debug(
            "(Location %d/%d) Got %d encounters at location %s",
            i_loc,
            len(locations),
            len(encounters),
            location["display_name"],
        )
        for i_enc, encounter in enumerate(encounters):
            logger.debug(
                "(%d/%d) Posting observations for encounter with UUID %s",
                i_enc,
                len(encounters),
                encounter["encounter_uuid"],
            )
            _populate_observations(clients, encounter)
        i_loc += 1


def populate_dhos_fuego(clients: ClientRepository) -> None:
    system_jwt = auth_controller.get_system_jwt()
    logger.debug("Populating dhos-fuego-api (FHIR EPR) with GDm patients")

    # we get patients from dhos-services and will use ALL of them that are
    #   active to populate FHIR EPR
    gdm_patients = services_client.search_patients(
        clients=clients,
        product_name="GDM",
        active=True,
        system_jwt=system_jwt,
    )

    # as dhos-services patient format differs from the one in dhos-fuego,
    #   we should transform it
    fhir_gdm_patients: List[Dict] = [
        generator_controller.generate_fhir_patient_from_dhos_services_patient(p)
        for p in gdm_patients
    ]

    logger.debug(
        "Populating dhos-fuego-api (FHIR EPR) with %d GDm patients",
        len(fhir_gdm_patients),
    )

    if len(fhir_gdm_patients) != len(gdm_patients):
        raise ValueError("Patients quantity mismatch!")

    fhir_patients_posted: List[Dict] = []
    for fhir_dhos_services_patient in fhir_gdm_patients:
        fhir_patient = fuego_client.create_fhir_patient(
            clients=clients,
            fhir_patient=fhir_dhos_services_patient,
            system_jwt=system_jwt,
        )
        fhir_patients_posted.append(fhir_patient)

    if len(fhir_patients_posted) != len(fhir_gdm_patients):
        raise ValueError("Patients quantity mismatch!")

    # now we should update dhos-services patients, providing fhir_resource_id
    logger.debug("Updating dhos-services patients with fhir_resource_id")
    fhir_posted: Dict = {p.pop("mrn"): p for p in fhir_patients_posted}
    for patient in gdm_patients:
        if patient["hospital_number"] not in fhir_posted:
            continue

        patient_details = {
            "fhir_resource_id": fhir_posted[patient["hospital_number"]][
                "fhir_resource_id"
            ]
        }

        logger.debug("Updating patient %s", patient["uuid"], extra=patient_details)
        services_client.update_patient(
            clients=clients,
            patient_id=patient["uuid"],
            patient_details=patient_details,
            jwt=system_jwt,
        )

    logger.debug(
        "dhos-fuego-api has been successfully populated with %d patients",
        len(fhir_patients_posted),
    )


def make_location(
    location_type: str = HOSPITAL_SCT_CODE,
    parent: Optional[Dict] = None,
    suffix: Optional[str] = None,
) -> Dict:
    if parent is None and location_type != HOSPITAL_SCT_CODE:
        raise ValueError(
            f"Cannot create a location of type {location_type} without parent"
        )

    if location_type == HOSPITAL_SCT_CODE:
        display_name = f"{fake.county()} Hospital"
    elif location_type == WARD_SCT_CODE:
        display_name = (
            f"Ward {suffix}"
            if suffix
            else f"{parent['display_name']} {fake.county()} Ward"  # type: ignore
        )
    elif location_type == BAY_SCT_CODE:
        display_name = (
            f"Bay {suffix}"
            if suffix
            else f"{parent['display_name']} {fake.county()} Bay"  # type: ignore
        )
    elif location_type == BED_SCT_CODE:
        display_name = (
            f"Bed {suffix}"
            if suffix
            else f"{parent['display_name']} {fake.county()} Bed"  # type: ignore
        )
    else:
        raise ValueError(f"Unknown location type: {location_type}")

    return {
        "uuid": str(uuid.uuid4()),
        "location_type": location_type,
        "ods_code": fake.license_plate().replace(" ", ""),
        "display_name": display_name,
        "dh_products": [{"product_name": "SEND", "opened_date": "2017-10-19"}],
        "active": True,
        "parent": parent["uuid"] if parent else None,
    }


def _open_and_closed_patients(
    clients: ClientRepository, num_patients: int, product_code: str
) -> List[Dict]:
    prefix = product_code.lower() + "_" if product_code != "GDM" else ""
    num_closed_patients = num_patients // 6
    num_open_patients = num_patients - num_closed_patients
    patients = [
        generator_controller.generate_patient(
            clients=clients,
            product_name=product_code,
            closed=False,
            uuid=f"static_{prefix}patient_uuid_{i}" if i < 10 else None,
            hospital_number=str(i) * 6 if i < 10 else None,
        )
        for i in range(num_open_patients)
    ]

    patients += [
        generator_controller.generate_patient(
            clients=clients, product_name=product_code, closed=True
        )
        for _ in range(num_closed_patients)
    ]
    return patients


def _populate_observations(clients: ClientRepository, encounter: Dict) -> None:
    admission_time = parse_iso8601_to_datetime(encounter["admitted_at"])
    if admission_time is None:
        raise ValueError("No admission time in encounter")
    admission_time = admission_time + timedelta(microseconds=500)
    current_time = datetime.utcnow().replace(tzinfo=timezone.utc)
    if encounter["discharged_at"] is not None:
        discharged_at = parse_iso8601_to_datetime(encounter["discharged_at"])
        if discharged_at is None:
            raise ValueError("Couldn't convert discharged_at timestamp")
        current_time = discharged_at - timedelta(microseconds=500)

    current_spo2_scale: int = encounter.get("spo2_scale", 1)
    logger.debug("Creating patient observations")
    clinician_jwt: str = _get_stan_lee_jwt()

    patient_trajectory: Trajectory = ObservationsGenerator.get_random_trajectory()

    obs_sets: List[Dict] = []

    rand_num_of_obs = random.choice(WEIGHTED_RANDOM)
    while current_time > admission_time and (len(obs_sets)) < rand_num_of_obs:

        if patient_trajectory == Trajectory.VERY_ILL:
            gap_minutes = random.randint(15, 90)
        elif patient_trajectory == Trajectory.MEDIUM_ILL:
            gap_minutes = random.randint(90, 3 * 60)
        elif patient_trajectory == Trajectory.A_BIT_ILL:
            gap_minutes = random.randint(3 * 60, 8 * 60)
        else:
            gap_minutes = random.randint(8 * 60, 24 * 60)
        current_time -= timedelta(minutes=gap_minutes)

        if random.random() > 0.9:
            # 10% chance of missed observations.
            continue

        current_obs_set: Dict = ObservationsGenerator.generate(
            encounter_id=encounter["encounter_uuid"],
            record_time=current_time,
            trajectory=patient_trajectory,
            current_spo2_scale=current_spo2_scale,
        )
        # If the observations are empty, skip them this time.
        if not current_obs_set.get("observations", []):
            logger.debug("No obs in set, skipping")
            continue

        obs_sets.append(current_obs_set)

    logger.debug("Posting %d observation sets", len(obs_sets))
    for idx, obs_set in enumerate(obs_sets):
        logger.debug("Posting observation set %d/%d", idx + 1, len(obs_sets))
        should_suppress = idx < len(obs_sets) - 1
        send_bff_client.create_observation(
            clients=clients,
            obs_set=obs_set,
            suppress_obs_publish=should_suppress,
            clinician_jwt=clinician_jwt,
        )
