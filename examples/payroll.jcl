//PAYRUN   JOB (FIN),'PAYROLL EXTRACT',CLASS=A,MSGCLASS=X
//*
//* The other half of examples/payroll.ezt.  Easytrieve names ddnames -
//* PERSNL, PAYEXT - and nothing else; THIS is where they become datasets.
//*
//* Note what the step's EXEC PGM= actually says: EZTPA00, the Easytrieve
//* interpreter, not PAYROLL.  Which Easytrieve program runs is decided by
//* the SYSIN member, which is why bind_jcl_ddnames matches on that rather
//* than on the step's program name.
//*
//RUNPAY   EXEC PGM=EZTPA00,REGION=4M
//STEPLIB  DD  DSN=PROD.EZTLOAD,DISP=SHR
//SYSPRINT DD  SYSOUT=*
//SYSIN    DD  DSN=PROD.EZTSRC(PAYROLL),DISP=SHR
//PERSNL   DD  DSN=PROD.HR.PERSNL.MASTER,DISP=SHR
//PAYEXT   DD  DSN=PROD.FIN.PAY.EXTRACT(+1),
//             DISP=(NEW,CATLG,DELETE),
//             SPACE=(CYL,(5,2),RLSE)
//*
//* A second Easytrieve step in the same job, running a DIFFERENT program
//* against a DIFFERENT dataset through the SAME ddname.  Binding on ddname
//* alone would bind PERSNL to whichever of these came last.
//*
//RUNAUD   EXEC PGM=EZTPA00,REGION=4M
//STEPLIB  DD  DSN=PROD.EZTLOAD,DISP=SHR
//SYSPRINT DD  SYSOUT=*
//SYSIN    DD  DSN=PROD.EZTSRC(AUDITRPT),DISP=SHR
//PERSNL   DD  DSN=TEST.HR.PERSNL.SAMPLE,DISP=SHR
//
