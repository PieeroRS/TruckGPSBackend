from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import AiAlert, FuelLog, Incident, IncidentEvent, Trip, User, Vehicle, utcnow

DEFAULT_PASSWORD = "password"


def seed_if_empty(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(User)):
        return

    users = [
        User(
            full_name="Operaciones TruckGPS",
            email="operaciones@truckgps.pe",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="supervisor",
        ),
        User(
            full_name="Thomas Rivera",
            email="thomasrv543@gmail.com",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="supervisor",
        ),
        User(
            full_name="Admin TruckGPS",
            email="admin@truckgps.pe",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="admin",
        ),
        User(
            full_name="Carlos Mendoza",
            email="conductor@truckgps.pe",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="conductor",
        ),
    ]
    db.add_all(users)
    db.flush()

    vehicles = [
        Vehicle(
            placa="ABC-123",
            modelo="Volvo FH 540",
            estado="ok",
            ruta="Lima → Trujillo",
            kilometraje=142850,
            prox_mantenimiento="2026-06-12",
            lat=-12.0464,
            lng=-77.0428,
            velocidad=78,
            rendimiento_km_gal=9.5,
            tanque_gal=110,
        ),
        Vehicle(
            placa="DGR-456",
            modelo="Scania R 450",
            estado="warn",
            ruta="Arequipa → Cusco",
            kilometraje=98430,
            prox_mantenimiento="2026-05-18",
            lat=-16.409,
            lng=-71.537,
            velocidad=0,
            rendimiento_km_gal=10.0,
            tanque_gal=100,
        ),
        Vehicle(
            placa="PER-789",
            modelo="Mercedes Actros",
            estado="alert",
            ruta="Lima → Huancayo",
            kilometraje=215120,
            prox_mantenimiento="2026-05-09",
            lat=-11.9404,
            lng=-76.2356,
            velocidad=12,
            rendimiento_km_gal=8.5,
            tanque_gal=120,
        ),
    ]
    db.add_all(vehicles)
    db.flush()

    abc = next(v for v in vehicles if v.placa == "ABC-123")
    dgr = next(v for v in vehicles if v.placa == "DGR-456")
    per = next(v for v in vehicles if v.placa == "PER-789")

    db.add(Trip(vehicle_id=abc.id, ruta="Lima → Trujillo", progress_pct=68, status="active"))

    # Cargas de combustible de ejemplo (últimos días). DGR-456 rinde por debajo
    # de su referencia para que el módulo de consumo muestre una alerta.
    hoy = utcnow()
    demo_cargas = [
        (abc, 9, 141600, 60.0, True, "Grifo Primax Panamericana Norte"),
        (abc, 6, 142170, 59.0, True, "Grifo Repsol Huacho"),
        (abc, 2, 142740, 61.0, True, "Grifo Petroperú Chimbote"),
        (dgr, 8, 97200, 50.0, True, "Grifo Repsol Arequipa"),
        (dgr, 5, 97830, 79.0, True, "Grifo Primax Juliaca"),
        (dgr, 1, 98400, 72.0, True, "Grifo Petroperú Cusco"),
        (per, 7, 214300, 70.0, True, "Grifo Primax Ate"),
        (per, 3, 214890, 70.5, True, "Grifo Repsol La Oroya"),
    ]
    for veh, dias, odo, gal, lleno, grifo in demo_cargas:
        db.add(
            FuelLog(
                vehicle_id=veh.id,
                fecha=hoy - timedelta(days=dias),
                galones=gal,
                monto=round(gal * 16.5, 2),
                odometro_km=odo,
                tanque_lleno=lleno,
                grifo=grifo,
                notas="Dato de ejemplo",
            )
        )

    db.add_all(
        [
            AiAlert(
                titulo="Retraso en Carretera Central",
                descripcion="Tráfico sugiere retrasos de 35-50 min. Unidades ABC-123 y PER-789 afectadas.",
                severidad="high",
                icon="alert-triangle",
                placas_afectadas=["PER-789", "ABC-123"],
            ),
            AiAlert(
                titulo="Mantenimiento — DGR-456",
                descripcion="Revisión de transmisión recomendada en los próximos 5 días.",
                severidad="medium",
                icon="wrench",
                placas_afectadas=["DGR-456"],
            ),
            AiAlert(
                titulo="Optimización Lima → Trujillo",
                descripcion="Ruta alternativa por Cañete podría reducir 28 min.",
                severidad="low",
                icon="route",
                placas_afectadas=["ABC-123"],
            ),
        ]
    )

    now = utcnow()
    inc1 = Incident(
        id="INC-2026-0001",
        vehicle_id=per.id,
        titulo="Falla en sistema de frenos",
        categoria="Mecánico",
        severidad="high",
        estado="en-atencion",
        ubicacion="Carretera Central km 78, Chosica",
        lat=-11.9404,
        lng=-76.2356,
        descripcion="Pérdida de presión en frenos. Conductor reporta vibración al frenar.",
        asignado_a="Equipo Mecánico Lima Norte",
        es_panico=False,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=30),
    )
    inc2 = Incident(
        id="INC-2026-0002",
        vehicle_id=dgr.id,
        titulo="Vehículo detenido sin reportar",
        categoria="Operativo",
        severidad="medium",
        estado="reportado",
        ubicacion="Salida sur de Arequipa",
        lat=-16.409,
        lng=-71.537,
        descripcion="GPS muestra detención de 45 minutos sin reporte del conductor.",
        asignado_a=None,
        es_panico=False,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    db.add_all([inc1, inc2])
    db.flush()

    db.add_all(
        [
            IncidentEvent(incident_id=inc1.id, estado="reportado", notas="Incidencia registrada"),
            IncidentEvent(incident_id=inc1.id, estado="asignado", notas="Asignado a equipo mecánico"),
            IncidentEvent(incident_id=inc1.id, estado="en-atencion", notas="Técnico en camino"),
            IncidentEvent(incident_id=inc2.id, estado="reportado", notas="Incidencia registrada"),
        ]
    )
    db.commit()
