from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import datetime, timedelta
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
    print(f" {apagados} leituras antigas removidas (anteriores a {limite}).")

    # Libera para novo agendamento
    global timer_limpeza
    timer_limpeza = None

# ---------- Controle da cortina ----------
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
    # Pegando datas do query params
    start_date_str = request.GET.get('start')
    end_date_str = request.GET.get('end')

    # Defaults
    hoje = timezone.localdate()
    start_date = hoje
    end_date = hoje

    if start_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

    # Ajusta end_date para incluir o dia inteiro
    start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))

    leituras = HourlyAverage.objects.filter(timestamp__range=(start_dt, end_dt)).order_by('timestamp')

    dados = [
        {
            'temperature': leitura.temperature,
            'humidity': leitura.humidity,
            'timestamp': leitura.timestamp.isoformat()
        }
        for leitura in leituras
    ]
    logs = CurtainLog.objects.order_by('-timestamp')[:10]  # mostra os 50 mais recentes
    context = {
        'leituras_json': json.dumps(dados),
        'start_date': start_date.strftime("%Y-%m-%d"),
        'end_date': end_date.strftime("%Y-%m-%d"),
        'logs': logs, 
    }

    return render(request, 'historico.html', context)


# ---------- API de status ----------
def get_status_api(request):
    control = _ensure_control()
    
    device = request.GET.get("device")

    # Apenas o ESP32 atualiza o heartbeat
    if device == "esp32":
        control.last_esp_ping = timezone.now()
        control.save(update_fields=["last_esp_ping"])

    latest = SensorReading.objects.order_by('-timestamp').first()

# ----- MODO AUTOMÁTICO -----
    if latest and control.automatic_mode:

        temperatura = latest.temperature
        min_t = control.min_temperature
        max_t = control.max_temperature

        should_be_open = min_t <= temperatura <= max_t

        # Guarda o estado anterior
        estado_anterior_aberta = control.curtain_is_open
        estado_anterior_status = control.curtain_status

        # --- 1) Se está em STOP ---
        if control.curtain_status == "stop":

            # Se já está na posição desejada → não muda nada
            if should_be_open == control.curtain_is_open:
                pass
            else:
                # temperatura pede movimento
                if should_be_open:
                    control.curtain_status = "open"
                else:
                    control.curtain_status = "close"

        # --- 2) Lógica normal fora do STOP ---
        else:
            if should_be_open:
                control.curtain_is_open = True
                control.curtain_status = "open"
            else:
                control.curtain_is_open = False
                control.curtain_status = "close"

        #  Só salva SE algo realmente mudou
        if (
            estado_anterior_aberta != control.curtain_is_open or
            estado_anterior_status != control.curtain_status
        ):
            control.save()



    # ----- RESPOSTA DO STATUS -----
    result = {
        "curtain_is_open": bool(control.curtain_is_open),
        "automatic_mode": bool(control.automatic_mode),
        "min_temperature": control.min_temperature,
        "max_temperature": control.max_temperature,
        "curtain_status": control.curtain_status,
        "esp_online": esp_online(control),
        "latest_reading": None,
    }

    if latest:
        result["latest_reading"] = {
            "temperature": latest.temperature,
            "humidity": latest.humidity,
            "timestamp": latest.timestamp.isoformat(),
        }

    return JsonResponse(result)


# ---------- Recebe leituras do ESP32 ----------
@csrf_exempt
@require_POST
def sensor_data_api(request):
    global timer_limpeza

    try:
        payload = json.loads(request.body)
        temperature = float(payload.get('temperature'))
        humidity = float(payload.get('humidity'))

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
            # Média incremental
            total_temp = media_hora.temperature * media_hora.count + temperature
            total_hum = media_hora.humidity * media_hora.count + humidity
            media_hora.count += 1
            media_hora.temperature = total_temp / media_hora.count
            media_hora.humidity = total_hum / media_hora.count
            media_hora.save()
        else:
            # Se uma nova hora começou (nova média criada),
            # agenda a limpeza das leituras antigas em 1 hora
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
# ---------- Altera o estado da cortina do modo aumotático para manual e vice-versa ----------
@csrf_exempt

def toggle_automatic_mode(request):
    if request.method == "POST":
        control = GreenhouseControl.objects.first()
        if not control:
            control = GreenhouseControl.objects.create()

        data = json.loads(request.body.decode("utf-8"))
        control.automatic_mode = data.get("automatic_mode", True)
        control.save()
        return JsonResponse({"automatic_mode": control.automatic_mode})
    
    else:
        control = GreenhouseControl.objects.first()
        if not control:
            control = GreenhouseControl.objects.create()
        return JsonResponse({"automatic_mode": control.automatic_mode})
# ---------- Controle manual ----------
@login_required
@require_POST
def manual_control_api(request):
    control = _ensure_control()

    # Bloquear se o ESP estiver offline
    if not esp_online(control):
        return JsonResponse({
            "success": False,
            "message": "O ESP32 está offline — comandos desativados.",
            "esp_online": False,
        })
    try:
        payload = json.loads(request.body)
        action = payload.get('action')
        control = _ensure_control()

        # Desativa o modo automático SOMENTE se ele estiver ativo
        #if control.automatic_mode:
        #    control.automatic_mode = False

        if action == 'open':
            control.curtain_is_open = True
            control.curtain_status = 'open'
            action_label = "aberta"
        elif action == 'close':
            control.curtain_is_open = False
            control.curtain_status = 'close'
            action_label = "fechada"
        elif action == 'stop':
            # STOP enviado após término do movimento
            # Ajusta posição final conforme o movimento anterior
            if control.curtain_status == "open":
                control.curtain_is_open = True   # terminou de abrir
            elif control.curtain_status == "close":
                control.curtain_is_open = False  # terminou de fechar    
            control.curtain_status = 'stop'
            action_label = "parada"
        else:
            return JsonResponse({'success': False, 'message': 'Ação inválida.'}, status=400)

        control.automatic_mode = False
        control.save()
        latest = SensorReading.objects.order_by('-timestamp').first()
        CurtainLog.objects.create(
            action=action,
            temperature=latest.temperature if latest else 0,
            humidity=latest.humidity if latest else 0,
            triggered_by=request.user if request.user.is_authenticated else None,
        )
        return JsonResponse({
            'success': True,
            'message': f'Cortina {action_label}',
            'curtain_status': action,
            'curtain_is_open': control.curtain_is_open,
            'automatic_mode': control.automatic_mode
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
# ---------- Controle da cortina vindo do ESP32 (sem login e sem CSRF) ----------
@csrf_exempt
@require_POST
def manual_control_esp_api(request):
    control = _ensure_control()

    try:
        payload = json.loads(request.body.decode("utf-8"))
        action = payload.get("action")

        # Determina final_action por padrão
        final_action = action

        if action == "open":
            control.curtain_is_open = True
            control.curtain_status = "open"
            control.automatic_mode = False   # DESLIGA automático quando ESP manda open
            final_action = "open"

        elif action == "close":
            control.curtain_is_open = False
            control.curtain_status = "close"
            control.automatic_mode = False   # DESLIGA automático quando ESP manda close
            final_action = "close"

        elif action == "stop":
            # STOP enviado pelo ESP após término do movimento
            if control.curtain_status == "open":
                control.curtain_is_open = True
                final_action = "open"
            elif control.curtain_status == "close":
                control.curtain_is_open = False
                final_action = "close"
            else:
                final_action = "stop"

            control.curtain_status = "stop"
            control.automatic_mode = False

        else:
            return JsonResponse({"success": False, "message": "Ação inválida."}, status=400)

        control.save()

        # ---- evita logs duplicados ----
        # pega último log e ignora se for o mesmo action nos últimos N segundos
        from django.utils import timezone as dj_timezone
        janela_segundos = 5
        ultimo_log = CurtainLog.objects.order_by("-timestamp").first()
        criar_log = True

        # Não loga se a ação final é igual à posição atual DA MESMA FORMA
        if ultimo_log and ultimo_log.action == final_action:
            criar_log = False

        # EVITA LOGAR 'open' ou 'close' vindo do STOP se não houve mudança real
        # Ex: usuário já havia fechado e STOP só confirmou
        estado_atual = control.curtain_is_open
        if final_action == "open" and estado_atual is True:
            criar_log = False

        if final_action == "close" and estado_atual is False:
            criar_log = False

        # Cria log apenas se realmente mudou
        if criar_log:
            latest = SensorReading.objects.order_by("-timestamp").first()
            CurtainLog.objects.create(
                action=final_action,
                temperature=latest.temperature if latest else 0,
                humidity=latest.humidity if latest else 0,
                triggered_by=None
            )

        return JsonResponse({
            "success": True,
            "curtain_status": control.curtain_status,
            "curtain_is_open": control.curtain_is_open,
            "automatic_mode": control.automatic_mode,
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

