from django.db import models
from django.contrib.auth.models import User

class SensorReading(models.Model):
    temperature = models.FloatField()
    humidity = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.timestamp.strftime('%d/%m/%Y %H:%M')} - T: {self.temperature}°C, H: {self.humidity}%"


class HourlyAverage(models.Model):
    timestamp = models.DateTimeField(unique=True)
    temperature = models.FloatField()
    humidity = models.FloatField()
    count = models.IntegerField(default=1)  # Para cálculo incremental da média

    def __str__(self):
        return f"{self.timestamp.strftime('%d/%m/%Y %H:%M')} - T: {self.temperature:.2f}°C, H: {self.humidity:.2f}%"


class GreenhouseControl(models.Model):
    """Armazena o estado da estufa e parâmetros"""
    min_temperature = models.FloatField(default=22.0)
    max_temperature = models.FloatField(default=30.0)
    curtain_is_open = models.BooleanField(default=False)
    curtain_status = models.CharField(max_length=10, default='stop')
    automatic_mode = models.BooleanField(default=True)
    singleton = models.BooleanField(default=True, editable=False, unique=True)
    last_esp_ping = models.DateTimeField(null=True, blank=True)


    def __str__(self):
        status = "Aberta" if self.curtain_is_open else "Fechada"
        return f"Configuração da Estufa - Faixa Ideal: {self.min_temperature}°C a {self.max_temperature}°C, Cortina: {status}"

class CurtainLog(models.Model):
    ACTION_CHOICES = [
        ('open', 'Aberta'),
        ('stop', 'Parada'),
        ('close', 'Fechada'),
    ]

    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    temperature = models.FloatField()
    humidity = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)
    triggered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.get_action_display()} em {self.timestamp.strftime('%d/%m %H:%M')}"