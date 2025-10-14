import time
import random
from django.core.management.base import BaseCommand
from greenhouse.models import SensorReading, GreenhouseControl

class Command(BaseCommand):
    help = 'Executa a lógica de simulação da estufa inteligente'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS('Iniciando simulação da estufa...'))

        control, _ = GreenhouseControl.objects.get_or_create(singleton=True)

        while True:
            # 1. Simular leitura do sensor
            current_temp = round(random.uniform(15.0, 35.0), 2)
            current_humidity = round(random.uniform(40.0, 80.0), 2)

            SensorReading.objects.create(temperature=current_temp, humidity=current_humidity)
            self.stdout.write(f'Nova leitura: Temp={current_temp}°C, Umidade={current_humidity}%')

            # 2. Obter a configuração atual
            control.refresh_from_db()
            desired_temp = control.desired_temperature

            # 3. Aplicar a lógica de controle
            # Se a temperatura atual for maior que a desejada, abre a cortina.
            # Se for menor, fecha a cortina.
            if current_temp > desired_temp:
                if not control.curtain_is_open:
                    control.curtain_is_open = True
                    control.save()
                    self.stdout.write(self.style.WARNING('Temperatura alta! Abrindo a cortina.'))
            else:
                if control.curtain_is_open:
                    control.curtain_is_open = False
                    control.save()
                    self.stdout.write(self.style.SUCCESS('Temperatura OK! Fechando a cortina.'))
            
            # Espera 10 segundos para a próxima simulação
            time.sleep(10)