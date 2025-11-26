from django.shortcuts import render
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models.functions import TruncHour
from django.db.models import Avg
from datetime import datetime, timedelta, time
import json
import threading

from .models import SensorReading, HourlyAverage, GreenhouseControl, CurtainLog

# Guarda o timer ativo para evitar agendamentos duplicados
timer_limpeza = None

def limpar_leituras_antigas():
    """Apaga apenas registros de leitura com mais de 1 hora."""
    from .models import SensorReading
    limite = timezone.now() - timedelta(hours=1)
    apagados, _ = SensorReading.objects.filter(timestamp__lt=limite).delete()
    print(f"{apagados} leituras antigas removidas (anteriores a {limite}).")

    # Libera para novo agendamento
    global timer_limpeza
    timer_limpeza = None

# ---------- Controle ----------
def _ensure_control():
    control = GreenhouseControl.objects.first()
    if not control:
        control = GreenhouseControl.objects.create()
    return control

def esp_online(control):
    """Retorna True se o ESP enviou ping nos últimos 20 segundos."""
    if not control.last_esp_ping:
        return False
    return (timezone.now() - control.last_esp_ping).total_seconds() < 20

@login_required
def dashboard_view(request):
    control = _ensure_control()
    return render(request, 'dashboard.html', {'control': control})

@login_required
def historico(request):
    # filtros de datas vindos da URL
    start_date_str = request.GET.get('start')
    end_date_str = request.GET.get('end')

    today = timezone.localdate()

    if start_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    else:
        # padrão: últimos 7 dias
        start_date = today - timedelta(days=7)

    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        end_date = today

    # datas como datetime com timezone
    tz = timezone.get_current_timezone()
    start_dt = datetime.combine(start_date, time.min, tzinfo=tz)
    end_dt = datetime.combine(end_date, time.max, tzinfo=tz)

    # === leituras de sensor para o gráfico ===
    leituras = (
        HourlyAverage.objects
        .filter(timestamp__range=(start_dt, end_dt))
        .order_by("timestamp")
    )

    dados = [
        {
            "timestamp": leitura.timestamp.isoformat(),
            "temperature": round(leitura.temperature, 2),
            "humidity": round(leitura.humidity, 2),
        }
        for leitura in leituras
    ]
    # === últimos 10 logs (sem 'stop'), já com texto pronto ===
    logs_qs = (
        CurtainLog.objects
        .exclude(action="stop")
        .order_by("-timestamp")[:10]
    )

    logs = []
    for log in logs_qs:
        if log.side == "left":
            lado = "cortina esquerda"
        elif log.side == "right":
            lado = "cortina direita"
        else:
            lado = "cortinas"

        if log.action == "open":
            acao = "abriu"
        elif log.action == "close":
            acao = "fechou"
        else:
            acao = log.action

        if log.triggered_by:
            modo = "modo manual"
            usuario = log.triggered_by.username
        else:
            modo = "modo automático"
            usuario = "Automático"

        if acao == "abriu":
            status = "Aberta"
        else:
            status = "Fechada"

        logs.append({
            "timestamp": log.timestamp,
            "status": status,          # ← ex: "Fechada"
            "lado": lado,              # ← ex: "cortina direita"
            "modo": modo,              # ← ex: "modo manual"
            "temperature": log.temperature,
            "humidity": log.humidity,
            "usuario": usuario,
        })

    context = {
        "leituras_json": json.dumps(dados),
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "logs": logs,
    }
    return render(request, "historico.html", context)


# ---------- API de status (ESP e browser consultam esse endpoint) ----------
@require_GET
def get_status_api(request):
   
    control = _ensure_control()

    device = request.GET.get("device")

    # === HEARTBEAT DO ESP ===
    if device == "esp32":
        control.last_esp_ping = timezone.now()
        control.esp_ip = request.META.get("REMOTE_ADDR", "desconhecido")
        control.save(update_fields=["last_esp_ping", "esp_ip"])

    # calcula se o esp está online
    esp_is_online = esp_online(control)

    # tempo desde último contato
    last_contact_seconds = None
    if control.last_esp_ping:
        last_contact_seconds = int((timezone.now() - control.last_esp_ping).total_seconds())

    # === CALCULA FAIL-SAFE ===
    fail_safe_active = False
    if not esp_is_online:
        # ESP sumiu — ativar failsafe
        fail_safe_active = True

    # === LER ÚLTIMA LEITURA DO SENSOR ===
    latest = SensorReading.objects.order_by('-timestamp').first()

    # === LÓGICA DO MODO AUTOMÁTICO ===
    if latest and control.automatic_mode and esp_is_online:
        temperatura = latest.temperature
        min_t = control.min_temperature
        max_t = control.max_temperature

        should_open = (min_t <= temperatura <= max_t)

        if should_open:
            # abre só se ainda não estiver totalmente aberta
            if not (control.left_is_open and control.right_is_open):
                desired_action = 'open'
            else:
                desired_action = 'stop'
        else:
            # fecha só se estiver aberta
            if control.left_is_open or control.right_is_open:
                desired_action = 'close'
            else:
                desired_action = 'stop'

        # guarda o status anterior para saber se o comando mudou
        previous_status = control.curtain_status

        control.auto_left_action = desired_action
        control.auto_right_action = desired_action
        control.curtain_status = desired_action
        control.save(update_fields=["auto_left_action", "auto_right_action", "curtain_status"])

        # REGISTRA LOG AUTOMÁTICO SOMENTE QUANDO O COMANDO MUDA
        if desired_action in ['open', 'close'] and desired_action != previous_status:
            temp = latest.temperature
            hum = latest.humidity

            # evita log idêntico em sequência
            ultimo_log = CurtainLog.objects.order_by("-timestamp").first()
            criar_log = True
            if (
                ultimo_log and
                ultimo_log.action == desired_action and
                ultimo_log.side in ['both'] and
                ultimo_log.triggered_by is None
            ):
                criar_log = False

            if criar_log:
                CurtainLog.objects.create(
                    side='both',              # ou 'left'/'right' se você quiser separar
                    action=desired_action,    # 'open' ou 'close'
                    temperature=temp,
                    humidity=hum,
                    triggered_by=None,        # marca como "modo automático" no histórico
                )

    # qual comando enviar para cada lado?
    if control.automatic_mode:
        left_action = control.auto_left_action
        right_action = control.auto_right_action
    else:
        left_action = control.manual_left_action
        right_action = control.manual_right_action

    # === PREPARA RESPOSTA ===
    response = {
        "esp_online": esp_is_online,
        "fail_safe": fail_safe_active,
        "esp_ip": control.esp_ip,
        "last_contact_seconds": last_contact_seconds,

        # posição real das cortinas (apenas confirmada quando ESP envia "stop")
        "left_is_open": control.left_is_open,
        "right_is_open": control.right_is_open,

        # comandos atuais
        "left": left_action,
        "right": right_action,
        "curtain_status": control.curtain_status,

        "automatic_mode": control.automatic_mode,

        # parâmetros automáticos
        "min_temperature": control.min_temperature,
        "max_temperature": control.max_temperature,

        "latest_reading": None,
    }

    if latest:
        response["latest_reading"] = {
            "temperature": latest.temperature,
            "humidity": latest.humidity,
            "timestamp": latest.timestamp.isoformat()
        }

    return JsonResponse(response)


# ---------- Recebe leituras do ESP32 ----------
@csrf_exempt
@require_POST
def sensor_data_api(request):
    global timer_limpeza

    try:
        payload = json.loads(request.body)
        temperature = float(payload.get('temperature') or payload.get('temp'))
        humidity = float(payload.get('humidity') or payload.get('hum') or payload.get('umidade'))


        # Salva leitura bruta
        SensorReading.objects.create(temperature=temperature, humidity=humidity)

        # Atualiza média horária
        agora = timezone.now()
        hora_inicio = agora.replace(minute=0, second=0, microsecond=0)

        media_hora, created = HourlyAverage.objects.get_or_create(
            timestamp=hora_inicio,
            defaults={'temperature': temperature, 'humidity': humidity, 'count': 1}
        )

        if not created:
            total_temp = media_hora.temperature * media_hora.count + temperature
            total_hum = media_hora.humidity * media_hora.count + humidity
            media_hora.count += 1
            media_hora.temperature = total_temp / media_hora.count
            media_hora.humidity = total_hum / media_hora.count
            media_hora.save()
        else:
            # Agendar limpeza das leituras antigas em 1 hora se não agendado
            if timer_limpeza is None or not timer_limpeza.is_alive():
                timer_limpeza = threading.Timer(3600, limpar_leituras_antigas)
                timer_limpeza.daemon = True
                timer_limpeza.start()
                print("Limpeza das leituras antigas agendada para 1 hora.")
            else:
                print("Já há uma limpeza agendada — não criou outra.")

        return JsonResponse({'success': True})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


# ---------- Atualiza parâmetros ----------
@login_required
@require_POST
def set_parameters_api(request):
    try:
        payload = json.loads(request.body)
        min_t = float(payload.get('min_temperature'))
        max_t = float(payload.get('max_temperature'))
        control = _ensure_control()
        control.min_temperature = min_t
        control.max_temperature = max_t
        control.save()
        return JsonResponse({'success': True, 'message': 'Parâmetros atualizados!'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


# ---------- Toggle automatic mode ----------
@csrf_exempt
def toggle_automatic_mode(request):
    if request.method == "POST":
        control = _ensure_control()
        try:
            data = json.loads(request.body.decode("utf-8"))
            automatic_mode = bool(data.get("automatic_mode", True))
            control.automatic_mode = automatic_mode
            # When switching to automatic, reset manual actions to stop
            if automatic_mode:
                control.manual_left_action = 'stop'
                control.manual_right_action = 'stop'
            control.save()
            return JsonResponse({"automatic_mode": control.automatic_mode})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
    else:
        control = _ensure_control()
        return JsonResponse({"automatic_mode": control.automatic_mode})


# ---------- Controle manual para cada cortina (web UI) ----------
@login_required
@require_POST
def manual_left_api(request):
    """
    POST: {"action":"open"|"close"|"stop"}
    """
    control = _ensure_control()

    if not esp_online(control):
        return JsonResponse(
            {
                "success": False,
                "message": "ESP32 está offline — comandos desativados.",
                "esp_online": False,
            }
        )

    try:
        payload = json.loads(request.body)
        action = payload.get("action")
        if action not in ["open", "close", "stop"]:
            return HttpResponseBadRequest("Ação inválida")

        # SEM mais checagem de left_is_open (sempre envia o comando)
        control.manual_left_action = action
        control.automatic_mode = False
        control.save()

        # Log simples sempre que um comando manual é enviado
        latest = SensorReading.objects.order_by("-timestamp").first()
        CurtainLog.objects.create(
            side="left",
            action=action,
            temperature=latest.temperature if latest else 0,
            humidity=latest.humidity if latest else 0,
            triggered_by=request.user if request.user.is_authenticated else None,
        )

        # Mensagem padrão
        if action == "open":
            mensagem = "Comando enviado para abrir a cortina esquerda."
        elif action == "close":
            mensagem = "Comando enviado para fechar a cortina esquerda."
        else:
            mensagem = "Comando de parada enviado para a cortina esquerda."

        return JsonResponse(
            {
                "success": True,
                "left": action,
                "message": mensagem,
            }
        )

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@login_required
@require_POST
def manual_right_api(request):
    """
    POST: {"action":"open"|"close"|"stop"}
    """
    control = _ensure_control()

    if not esp_online(control):
        return JsonResponse(
            {
                "success": False,
                "message": "ESP32 está offline — comandos desativados.",
                "esp_online": False,
            }
        )

    try:
        payload = json.loads(request.body)
        action = payload.get("action")
        if action not in ["open", "close", "stop"]:
            return HttpResponseBadRequest("Ação inválida")

        # SEM mais checagem de right_is_open (sempre envia o comando)
        control.manual_right_action = action
        control.automatic_mode = False
        control.save()

        # Log simples sempre que um comando manual é enviado
        latest = SensorReading.objects.order_by("-timestamp").first()
        CurtainLog.objects.create(
            side="right",
            action=action,
            temperature=latest.temperature if latest else 0,
            humidity=latest.humidity if latest else 0,
            triggered_by=request.user if request.user.is_authenticated else None,
        )

        # Mensagem padrão
        if action == "open":
            mensagem = "Comando enviado para abrir a cortina direita."
        elif action == "close":
            mensagem = "Comando enviado para fechar a cortina direita."
        else:
            mensagem = "Comando de parada enviado para a cortina direita."

        return JsonResponse(
            {
                "success": True,
                "right": action,
                "message": mensagem,
            }
        )

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


# ---------- Controle vindo do ESP32 (sem login e sem CSRF) ----------
@csrf_exempt
@require_POST
def manual_control_esp_api(request):
    """
    Endpoint que o ESP chama para confirmar ações/stop.
    Espera JSON: {"side":"left"|"right"|"both", "action":"open"|"close"|"stop"}
    Quando action == 'stop' o ESP está confirmando posição final — atualizamos left_is_open/right_is_open.
    """
    control = _ensure_control()
    try:
        payload = json.loads(request.body.decode("utf-8"))
        side = payload.get("side", "both")
        action = payload.get("action")

        if action not in ['open', 'close', 'stop']:
            return JsonResponse({"success": False, "message": "Ação inválida."}, status=400)

        latest = SensorReading.objects.order_by("-timestamp").first()
        temp = latest.temperature if latest else 0
        hum = latest.humidity if latest else 0

        final_action = action

        if action == "stop":
            # Atualiza posição física com base no último comando enviado
            if side in ['left', 'both']:
                last_left_cmd = (
                    control.manual_left_action
                    if not control.automatic_mode
                    else control.auto_left_action
                )
                if last_left_cmd == 'open':
                    control.left_is_open = True
                elif last_left_cmd == 'close':
                    control.left_is_open = False

                # limpa comandos da esquerda para não reenviar open/close depois do stop
                control.manual_left_action = 'stop'
                control.auto_left_action = 'stop'

            if side in ['right', 'both']:
                last_right_cmd = (
                    control.manual_right_action
                    if not control.automatic_mode
                    else control.auto_right_action
                )
                if last_right_cmd == 'open':
                    control.right_is_open = True
                elif last_right_cmd == 'close':
                    control.right_is_open = False

                # limpa comandos da direita
                control.manual_right_action = 'stop'
                control.auto_right_action = 'stop'

            control.curtain_status = 'stop'
            control.curtain_is_open = (control.left_is_open and control.right_is_open)

        else:
            # ESP enviou open/close (iniciando movimento)
            control.curtain_status = action
            # não mexe em automatic_mode aqui

        control.save()

        # Evita log duplicado
        ultimo_log = CurtainLog.objects.order_by("-timestamp").first()
        criar_log = True
        is_manual = not control.automatic_mode
        triggered_by_user = None

        if ultimo_log:
            if ultimo_log.action == final_action and ultimo_log.side == side:
                criar_log = False

        if criar_log:
            CurtainLog.objects.create(
                side=side if side in ['left', 'right', 'both'] else 'both',
                action=final_action,
                temperature=temp,
                humidity=hum,
                triggered_by=triggered_by_user  # None = Automático
            )

        return JsonResponse({
            "success": True,
            "curtain_status": control.curtain_status,
            "left_is_open": control.left_is_open,
            "right_is_open": control.right_is_open,
            "automatic_mode": control.automatic_mode,
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

