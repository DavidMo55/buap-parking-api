import os
import json
import uuid
import time
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from google import genai
from google.genai import types
from azure.storage.blob import BlobServiceClient

load_dotenv()

# Configuración de Clientes
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("PUBLIC_SUPABASE_ANON_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
AZURE_CONNECTION = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
gemini_client = genai.Client(api_key=GEMINI_KEY)
blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def upload_to_azure(file_bytes: bytes, filename: str) -> str:
    ext = filename.split(".")[-1] if "." in filename else "jpg"
    unique_name = f"parking/{datetime.now().strftime('%Y/%m/%d')}/{uuid.uuid4()}.{ext}"
    blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=unique_name)
    blob_client.upload_blob(file_bytes, overwrite=True)
    return blob_client.url

@app.post("/gestionar-vehiculo")
async def gestionar_vehiculo(
    file: UploadFile = File(None),
    placa: str = Form(None),
    nivel: int = Form(None),
    cajon: int = Form(None),
    marca: str = Form(None),
    modelo: str = Form(None),
    color: str = Form(None),
    tipo: str = Form(None),
    usar_ia: bool = Form(False)
):
    try:
        foto_url = None
        data_ia = {}
        
        # 1. Normalizar placa de entrada manual
        input_placa = placa.replace("-", "").replace(" ", "").upper().strip() if placa else None

        # 2. Obtener ocupación actual para dársela a la IA
        res_dispo = supabase.table("historial_estacionamiento").select("nivel, cajon").eq("estado", "dentro").execute()
        ocupados = res_dispo.data or []

        # 3. Procesar Archivo e IA
        if file:
            image_bytes = await file.read()
            foto_url = await upload_to_azure(image_bytes, file.filename)
            
            if usar_ia:
                try:
                    # Usamos Gemini 2.5 Flash
                    prompt = f"""
                    Analiza la imagen y responde SOLO JSON. 
                    OCUPADOS: {ocupados}
                    REGLAS:
                    - Tipo: Debe ser 'compacto', 'camioneta' o 'lujo'.
                    - Camioneta/SUV: nivel 1.
                    - Sedan/Compacto: nivel 2.
                    - Lujo/Deportivo: nivel 3.
                    - Cajon: 1-20 (no ocupado).
                    - Placa: Si no se ve, usa 'UNK-000'.
                    JSON: {{"marca": "str", "modelo": "str", "color": "str", "placa": "str", "tipo": "str", "nivel": int, "cajon": int}}
                    """
                    
                    response = gemini_client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=[
                            prompt,
                            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
                        ],
                        config=types.GenerateContentConfig(response_mime_type="application/json")
                    )
                    data_ia = json.loads(response.text)
                except Exception as e:
                    print(f"⚠️ IA Error: {e}")
                    data_ia = {"placa": f"TICK-{int(time.time() % 100000)}"}

        # 4. Determinar Placa Final
        final_placa = (input_placa or data_ia.get("placa", "UNK-000")).replace("-", "").replace(" ", "").upper().strip()
        if "UNK" in final_placa or "FAIL" in final_placa:
            final_placa = f"EXT-{int(time.time() % 1000000)}"

        # 5. Persistencia de Foto
        foto_final = foto_url
        if not foto_final:
            res_v = supabase.table("vehiculos").select("ultima_foto").eq("placa", final_placa).execute()
            if res_v.data:
                foto_final = res_v.data[0].get("ultima_foto")

        # 6. Mapeo de Seguridad para 'tipo' (Evita el error 23514)
        # Esto asegura que lo que mandemos a Supabase sea aceptado por el Check Constraint
        tipo_ia = (tipo or data_ia.get("tipo", "compacto")).lower()
        
        if any(keyword in tipo_ia for keyword in ["camioneta", "suv", "van", "pick", "grande"]):
            tipo_validado = "camioneta"
        elif any(keyword in tipo_ia for keyword in ["lujo", "deportivo", "sport", "racing", "premium"]):
            tipo_validado = "lujo"
        else:
            tipo_validado = "compacto"

        # 7. Upsert en Tabla Maestra de Vehículos
        vehiculo_data = {
            "placa": final_placa,
            "marca": marca or data_ia.get("marca", "Desconocido"),
            "modelo": modelo or data_ia.get("modelo", "Desconocido"),
            "color": color or data_ia.get("color", "N/A"),
            "tipo": tipo_validado,
            "ultima_foto": foto_final
        }
        
        supabase.table("vehiculos").upsert(vehiculo_data, on_conflict="placa").execute()

        # 8. Insertar en Historial (Movimiento actual)
        historial_data = {
            "placa": final_placa,
            "nivel": nivel or data_ia.get("nivel", 1),
            "cajon": cajon or data_ia.get("cajon", 1),
            "foto_url": foto_final,
            "estado": "dentro",
            "fecha_ingreso": datetime.now().isoformat()
        }
        
        db_res = supabase.table("historial_estacionamiento").insert(historial_data).execute()

        return {
            "status": "success",
            "data": {
                "vehiculo": vehiculo_data,
                "historial_id": db_res.data[0]['id'],
                "foto_url": foto_final
            }
        }

    except Exception as e:
        print(f"❌ Error Crítico: {str(e)}")
        # Si el error es de restricción de base de datos, lo imprimimos detallado
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/registrar-salida/{historial_id}")
async def registrar_salida(historial_id: str):
    try:
        res = supabase.table("historial_estacionamiento").update({
            "estado": "fuera",
            "fecha_salida": datetime.now().isoformat()
        }).eq("id", historial_id).execute()

        if not res.data:
            raise HTTPException(status_code=404, detail="Registro no encontrado")

        return {"status": "success", "message": "Salida registrada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/vehiculo/{placa}")
async def obtener_detalle_vehiculo(placa: str):
    try:
        placa_clean = placa.upper().strip()
        vehiculo = supabase.table("vehiculos").select("*").eq("placa", placa_clean).single().execute()
        historial = supabase.table("historial_estacionamiento").select("*").eq("placa", placa_clean).order("fecha_ingreso", desc=True).execute()
        
        return {
            "perfil": vehiculo.data,
            "historial_fotos": historial.data
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail="Vehículo no encontrado")
    
@app.get("/vehiculo-por-registro/{historial_id}")
async def obtener_por_registro(historial_id: str):
    res_h = supabase.table("historial_estacionamiento").select("*").eq("id", historial_id).single().execute()
    if not res_h.data:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    
    res_v = supabase.table("vehiculos").select("*").eq("placa", res_h.data['placa']).single().execute()
    
    return {
        "registro": res_h.data,
        "vehiculo": res_v.data
    }

@app.get("/disponibilidad/{nivel}")
async def consultar_disponibilidad(nivel: int):
    try:
        # EL CAMBIO ESTÁ AQUÍ: Agregamos "id" al select
        res = supabase.table("historial_estacionamiento")\
            .select("id, cajon, placa")\
            .eq("nivel", nivel)\
            .eq("estado", "dentro")\
            .execute()
        
        ocupados_detalle = res.data or []
        ocupados_ids = [r['cajon'] for r in ocupados_detalle]
        libres = [c for c in range(1, 21) if c not in ocupados_ids]
        
        return {
            "nivel": nivel, 
            "libres": libres, 
            "ocupados_detalle": ocupados_detalle
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/test-db")
async def obtener_todos_los_vehiculos():
    try:
        res = supabase.table("vehiculos").select("*").execute()
        
        print(f"📊 Catálogo: Enviando {len(res.data)} vehículos al frontend")
        
        return res.data
    except Exception as e:
        print(f"❌ Error al consultar catálogo: {e}")
        return []