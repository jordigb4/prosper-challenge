import asyncio
from loguru import logger
from healthie import find_patient,create_appointment
from dotenv import load_dotenv

load_dotenv()

async def test_find_patient():
    
    # Similar names
    test1 = await find_patient("John Wright Dough", "2016-11-09")
    logger.debug(f"[Find Patient] Test 1 result {test1}")

    test2 = await find_patient("John Wright Doe", "2016-11-09")
    logger.debug(f"[Find Patient] Test 2 result {test2}")


async def test_create_appointment():

    # Already existing
    test1 = await create_appointment(
        patient_id='14025848',
        date='2026-03-11',
        time='13:00'
    )
    logger.debug(f"[Create Appointment] Test 1 result {test1}")

    # Invalid date (past)
    test2 = await create_appointment(
        patient_id='14025848',
        date='2025-03-11',
        time='13:00'
    )
    logger.debug(f"[Create Appointment] Test 2 result {test2}")

    
    # Invalid date (format)
    test3 = await create_appointment(
        patient_id='14025848',
        date='03-2025-11',
        time='13:00'
    )
    logger.debug(f"[Create Appointment] Test 3 result {test3}")


    # Valid date
    test4 = await create_appointment(
        patient_id='14025848',
        date='2026-03-11',
        time='01:00'
    )
    logger.debug(f"[Create Appointment] Test 4 result {test4}")

if __name__ == "__main__":
    asyncio.run(test_create_appointment())