img=pwmon
proj=pwmon

image:
	docker build -t ${img} .
	docker image list ${img}

srcup:
	docker cp ./nr.py ${proj}:/nr.py

container: 
	docker run -d --restart unless-stopped --name ${proj} --env-file env.list pwmon

attach:
	docker start ${proj}
	docker attach ${proj}
