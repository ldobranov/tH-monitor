from crontab import CronTab
cron = CronTab(user='pi')
job = cron.new(command='python /home/pi/lcd/read_temp.py')
job.setall('0,5,10,15,20,25,30,35,40,45,50,55 * * * *')
cron.write()
